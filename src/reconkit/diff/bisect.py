"""Checksum bisection across two databases.

Per level, each side answers one aggregate query (more only if the list of mismatching
buckets is too long for one statement) that returns ``(bucket, count, checksum)`` rows.
Buckets whose pair of (count, checksum) differs are either small enough to download and
compare locally, or are split again at the next level. Nothing else leaves the database.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TypeVar

from reconkit.adapters.base import Adapter, Row
from reconkit.diff.compare import ColumnRule, CompareOutcome, compare_rows
from reconkit.diff.model import DiffOptions, Key, RowDiff
from reconkit.diff.plan import HashPlan, Plan, RangePlan, auto_hash_buckets
from reconkit.diff.sql import SideSql
from reconkit.errors import DiffError

T = TypeVar("T")
Checksums = dict[int, tuple[int, int]]


@dataclass
class Side:
    adapter: Adapter
    sql: SideSql
    rows_fetched: int = 0
    row_count: int = 0


@dataclass
class BisectOutcome:
    strategy: str
    diffs: list[RowDiff] = field(default_factory=list)
    duplicate_keys: list[Key] = field(default_factory=list)
    within_tolerance: int = 0
    truncated: bool = False
    levels: int = 0


#: A hash-bucket list is an IN list of integers, which is cheap to evaluate, and every
#: statement in hash mode is a full scan. So hash bucketing packs this many times more
#: buckets into one statement than ``max_buckets_per_query`` allows for key ranges.
HASH_BATCH_FACTOR = 64


def _chunks(items: Sequence[int], size: int) -> list[Sequence[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class Bisector:
    def __init__(
        self,
        left: Side,
        right: Side,
        key_names: Sequence[str],
        rules: Sequence[ColumnRule],
        options: DiffOptions,
    ) -> None:
        self.left, self.right = left, right
        self.key_names, self.rules, self.options = key_names, rules, options
        # One connection cannot serve two queries at once; run sides in turn in that case.
        self._parallel = left.adapter is not right.adapter

    def _both(self, fn: Callable[[Side], T]) -> tuple[T, T]:
        if not self._parallel:
            return fn(self.left), fn(self.right)
        with ThreadPoolExecutor(max_workers=2) as pool:
            lf, rf = pool.submit(fn, self.left), pool.submit(fn, self.right)
            return lf.result(), rf.result()

    # -- planning -----------------------------------------------------------------------

    def range_plan(self) -> RangePlan | None:
        """Plan over the union of both sides' key bounds; None when both sides are empty."""

        def bounds(side: Side) -> tuple[int, int, int] | None:
            lo, hi, nulls, count = side.adapter.query(side.sql.minmax_sql())[0]
            if nulls:
                raise DiffError(
                    f"{side.sql.table}: {nulls} row(s) have a NULL key; range bucketing needs "
                    "non-NULL keys (filter them out or use bucketing: hash)"
                )
            return None if lo is None else (int(lo), int(hi), int(count))

        found = [b for b in self._both(bounds) if b is not None]
        if not found:
            return None
        opts = self.options
        lo, hi = min(b[0] for b in found), max(b[1] for b in found)
        if opts.initial_buckets is not None:
            return RangePlan.build(lo, hi, opts.bisection_factor, opts.initial_buckets)
        rows = max(b[2] for b in found)
        return RangePlan.for_rows(lo, hi, opts.bisection_factor, rows, opts.bisection_threshold)

    def hash_plan(self) -> HashPlan:
        opts = self.options
        initial = opts.initial_buckets
        if initial is None:
            counts = self._both(lambda side: int(side.adapter.query(side.sql.count_sql())[0][0]))
            initial = auto_hash_buckets(max(counts), opts.bisection_threshold)
        return HashPlan(opts.bisection_factor, initial)

    # -- execution ----------------------------------------------------------------------

    def _batch_size(self, plan: Plan) -> int:
        size = self.options.max_buckets_per_query
        return size * HASH_BATCH_FACTOR if isinstance(plan, HashPlan) else size

    def _checksums(
        self, plan: Plan, level: int, parents: Sequence[int] | None
    ) -> tuple[Checksums, Checksums]:
        batches: list[Sequence[int] | None] = (
            [None] if parents is None else list(_chunks(parents, self._batch_size(plan)))
        )

        def run(side: Side) -> Checksums:
            out: Checksums = {}
            for batch in batches:
                for bucket, count, total in side.adapter.query(
                    side.sql.checksum_sql(plan, level, batch)
                ):
                    out[int(bucket)] = (int(count), int(total or 0))
            return out

        return self._both(run)

    def _fetch(self, plan: Plan, level: int, buckets: Sequence[int]) -> CompareOutcome:
        def run(side: Side) -> list[Row]:
            rows = side.adapter.query(side.sql.fetch_sql(plan, level, buckets))
            side.rows_fetched += len(rows)
            return rows

        lrows, rrows = self._both(run)
        return compare_rows(lrows, rrows, self.key_names, self.rules)

    def run(self, plan: Plan) -> BisectOutcome:
        opts = self.options
        kind = "range" if isinstance(plan, RangePlan) else "hash"
        out = BisectOutcome(strategy=f"bisect/{kind}")
        level = 0
        left, right = self._checksums(plan, level, None)
        self.left.row_count = sum(n for n, _ in left.values())
        self.right.row_count = sum(n for n, _ in right.values())

        while True:
            out.levels = level + 1
            mismatched = sorted(
                b for b in left.keys() | right.keys() if left.get(b) != right.get(b)
            )
            leaves: list[int] = []
            split: list[int] = []
            for b in mismatched:
                rows = max(left.get(b, (0, 0))[0], right.get(b, (0, 0))[0])
                small = rows <= opts.bisection_threshold
                (leaves if small or plan.is_last(level) else split).append(b)

            for batch in _chunks(leaves, self._batch_size(plan)):
                found = self._fetch(plan, level, batch)
                out.diffs.extend(found.diffs)
                out.duplicate_keys.extend(found.duplicate_keys)
                out.within_tolerance += found.within_tolerance
                if opts.limit is not None and len(out.diffs) >= opts.limit:
                    out.diffs = out.diffs[: opts.limit]
                    out.truncated = True
                    return out
            if not split:
                return out
            level += 1
            left, right = self._checksums(plan, level, split)
