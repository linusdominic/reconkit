"""Local comparison of downloaded rows.

Rows arrive as tuples of canonical text (or None), keys first. Equal text means equal
values; unequal text is given a second chance against the column's tolerance. This is the
only place a difference is decided: checksums merely choose which rows get here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from reconkit.diff.model import ColumnDiff, Key, RowDiff, Tolerance
from reconkit.types import Canon, Kind

CanonRow = tuple[str | None, ...]


@dataclass(frozen=True)
class ColumnRule:
    name: str
    canon: Canon
    tolerance: Tolerance | None = None


@dataclass
class CompareOutcome:
    diffs: list[RowDiff] = field(default_factory=list)
    duplicate_keys: list[Key] = field(default_factory=list)
    within_tolerance: int = 0


def parse_timestamp(text: str) -> datetime:
    """Parse canonical ``YYYY-MM-DD[ HH:MM:SS[.f{1,6}]]`` text."""
    base, _, fraction = text.partition(".")
    fmt = "%Y-%m-%d %H:%M:%S" if " " in base else "%Y-%m-%d"
    parsed = datetime.strptime(base, fmt)
    if fraction:
        parsed = parsed.replace(microsecond=int(fraction.ljust(6, "0")[:6]))
    return parsed


def values_equal(left: str | None, right: str | None, rule: ColumnRule) -> bool:
    if left == right:
        return True
    tol = rule.tolerance
    if tol is None or left is None or right is None:
        return False
    kind = rule.canon.kind
    try:
        if kind in (Kind.DECIMAL, Kind.INTEGER):
            a, b = Decimal(left), Decimal(right)
            delta = abs(a - b)
            if tol.abs is not None and delta <= tol.abs:
                return True
            return tol.rel is not None and delta <= tol.rel * max(abs(a), abs(b))
        if kind in (Kind.TIMESTAMP, Kind.DATE) and tol.seconds is not None:
            gap = abs((parse_timestamp(left) - parse_timestamp(right)).total_seconds())
            return gap <= tol.seconds
    except (InvalidOperation, ValueError):
        return False
    return False


def sort_key(key: Key) -> tuple[tuple[int, int, str], ...]:
    """Order keys numerically where they look numeric, with NULLs first."""
    out: list[tuple[int, int, str]] = []
    for part in key:
        if part is None:
            out.append((0, 0, ""))
        elif isinstance(part, str) and part.lstrip("-").isdigit():
            out.append((1, int(part), ""))
        else:
            out.append((2, 0, str(part)))
    return tuple(out)


def _index(
    rows: Iterable[Sequence[str | None]], n_keys: int, duplicates: set[Key]
) -> dict[Key, CanonRow]:
    index: dict[Key, CanonRow] = {}
    for row in rows:
        key = tuple(row[:n_keys])
        if key in index:
            duplicates.add(key)
            continue
        index[key] = tuple(row)
    return index


def compare_rows(
    left_rows: Iterable[Sequence[str | None]],
    right_rows: Iterable[Sequence[str | None]],
    key_names: Sequence[str],
    rules: Sequence[ColumnRule],
) -> CompareOutcome:
    """Compare two sets of canonical rows that cover the same key buckets."""
    n_keys = len(key_names)
    names = [*key_names, *(r.name for r in rules)]
    duplicates: set[Key] = set()
    left = _index(left_rows, n_keys, duplicates)
    right = _index(right_rows, n_keys, duplicates)
    outcome = CompareOutcome(duplicate_keys=sorted(duplicates, key=sort_key))

    for key in sorted(left.keys() | right.keys(), key=sort_key):
        lrow, rrow = left.get(key), right.get(key)
        if lrow is None:
            assert rrow is not None
            outcome.diffs.append(
                RowDiff("missing_in_left", key, right=dict(zip(names, rrow, strict=True)))
            )
            continue
        if rrow is None:
            outcome.diffs.append(
                RowDiff("missing_in_right", key, left=dict(zip(names, lrow, strict=True)))
            )
            continue
        if lrow == rrow:
            continue
        changed = tuple(
            ColumnDiff(rule.name, lrow[n_keys + i], rrow[n_keys + i])
            for i, rule in enumerate(rules)
            if not values_equal(lrow[n_keys + i], rrow[n_keys + i], rule)
        )
        if changed:
            outcome.diffs.append(
                RowDiff(
                    "changed",
                    key,
                    left=dict(zip(names, lrow, strict=True)),
                    right=dict(zip(names, rrow, strict=True)),
                    columns=changed,
                )
            )
        else:
            outcome.within_tolerance += 1
    return outcome
