"""Match columns between the two tables and decide each pair's canonical form."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from reconkit.diff.model import DiffOptions, Tolerance
from reconkit.errors import DiffError
from reconkit.types import Canon, ColumnInfo, ColumnType, Kind

_NUMERIC = {Kind.INTEGER, Kind.DECIMAL, Kind.FLOAT, Kind.BOOLEAN}


@dataclass(frozen=True)
class ColumnPair:
    #: Name used in reports: the left table's column name.
    name: str
    left: ColumnInfo
    right: ColumnInfo
    canon: Canon
    tolerance: Tolerance | None = None


def _scale_of(col: ColumnType, float_precision: int) -> int | None:
    if col.kind is Kind.DECIMAL:
        return col.scale if col.scale is not None else float_precision
    if col.kind is Kind.FLOAT:
        return float_precision
    return None  # integers and booleans take the other side's scale


def resolve_canon(
    left: ColumnType, right: ColumnType, options: DiffOptions, precision: int | None = None
) -> Canon:
    """Pick the canonical form for a column pair.

    Numbers are compared at the coarser of the two sides' scales, because the finer side
    can always be rounded to the coarser one but not the other way round. ``precision``
    overrides that (decimal places for numbers, fractional digits for timestamps).
    """
    kinds = {left.kind, right.kind}
    text = Canon(
        Kind.TEXT, trim=options.trim_whitespace, empty_as_null=options.empty_string_as_null
    )
    if kinds == {Kind.BOOLEAN}:
        return Canon(Kind.BOOLEAN)
    if kinds <= {Kind.INTEGER, Kind.BOOLEAN}:
        return Canon(Kind.INTEGER)
    if kinds <= _NUMERIC:
        scales = [
            s
            for s in (
                _scale_of(left, options.float_precision),
                _scale_of(right, options.float_precision),
            )
            if s is not None
        ]
        scale = precision if precision is not None else min(scales)
        return Canon(Kind.DECIMAL, scale=max(0, min(scale, 37)))
    if kinds == {Kind.DATE}:
        return Canon(Kind.DATE)
    if kinds <= {Kind.DATE, Kind.TIMESTAMP}:
        digits = precision if precision is not None else options.timestamp_precision
        return Canon(
            Kind.TIMESTAMP,
            ts_precision=max(0, min(digits, 6)),
            shift_naive=Kind.DATE not in kinds,
        )
    if kinds <= {Kind.UUID, Kind.TEXT} and Kind.UUID in kinds:
        return Canon(Kind.UUID)
    return text


def _lookup(columns: Sequence[ColumnInfo]) -> dict[str, ColumnInfo]:
    return {c.name.lower(): c for c in columns}


def resolve_columns(
    left_columns: Sequence[ColumnInfo],
    right_columns: Sequence[ColumnInfo],
    key: Sequence[str],
    *,
    columns: Sequence[str] | None = None,
    ignore: Sequence[str] = (),
    mapping: Mapping[str, str] | None = None,
    tolerances: Mapping[str, Tolerance] | None = None,
    precision: Mapping[str, int] | None = None,
    options: DiffOptions,
) -> tuple[list[ColumnPair], list[ColumnPair], list[str]]:
    """Return (key pairs, value pairs, warnings).

    All names are the left table's names and are matched case-insensitively. ``mapping``
    maps a left column name to the right table's name for the same column.
    """
    if not key:
        raise DiffError("at least one key column is required")
    left, right = _lookup(left_columns), _lookup(right_columns)
    renames = {k.lower(): v.lower() for k, v in (mapping or {}).items()}
    tols = {k.lower(): v for k, v in (tolerances or {}).items()}
    precisions = {k.lower(): v for k, v in (precision or {}).items()}
    ignored = {c.lower() for c in ignore}
    keys = [k.lower() for k in key]
    warnings: list[str] = []

    for label, names in (("mapping", renames), ("tolerances", tols), ("precision", precisions)):
        unknown = sorted(set(names) - set(left))
        if unknown:
            raise DiffError(f"{label} refers to columns not in the left table: {unknown}")

    def pair(name: str, required: bool) -> ColumnPair | None:
        lcol = left.get(name)
        rcol = right.get(renames.get(name, name))
        if lcol is None or rcol is None:
            if required:
                side = "left" if lcol is None else "right"
                raise DiffError(f"column {name!r} not found in the {side} table")
            return None
        canon = resolve_canon(lcol.type, rcol.type, options, precisions.get(name))
        tolerance = tols.get(name)
        if tolerance is not None:
            numeric_ok = canon.kind in (Kind.DECIMAL, Kind.INTEGER) and (
                tolerance.abs is not None or tolerance.rel is not None
            )
            time_ok = canon.kind in (Kind.TIMESTAMP, Kind.DATE) and tolerance.seconds is not None
            if not (numeric_ok or time_ok):
                raise DiffError(
                    f"tolerance on {name!r} does not apply to a {canon.kind.value} column "
                    "(use abs/rel for numbers, seconds for dates and timestamps)"
                )
        if canon.kind is Kind.TEXT and {lcol.type.kind, rcol.type.kind} != {Kind.TEXT}:
            warnings.append(
                f"column {name!r}: no shared canonical form for {lcol.type.native} vs "
                f"{rcol.type.native}; comparing each engine's text cast"
            )
        return ColumnPair(lcol.name, lcol, rcol, canon, tolerance)

    key_pairs = [p for k in keys if (p := pair(k, required=True)) is not None]
    for p in key_pairs:
        if p.left.type.kind is Kind.FLOAT or p.right.type.kind is Kind.FLOAT:
            warnings.append(f"key column {p.name!r} is a float; keys should be exact types")

    if columns is not None:
        wanted = [c.lower() for c in columns if c.lower() not in keys]
        explicit = True
    else:
        wanted = [c for c in left if c not in keys]
        explicit = False
        mapped_right = {renames.get(c, c) for c in left}
        right_only = sorted(set(right) - mapped_right - ignored)
        if right_only:
            warnings.append(f"columns only in the right table were not compared: {right_only}")

    value_pairs: list[ColumnPair] = []
    for name in wanted:
        if name in ignored:
            continue
        p = pair(name, required=explicit)
        if p is None:
            warnings.append(f"column {name!r} only in the left table was not compared")
        else:
            value_pairs.append(p)
    return key_pairs, value_pairs, warnings
