"""Public entry point: resolve columns, pick a strategy, run it, assemble the result."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from reconkit.adapters.base import Adapter, QueryStats
from reconkit.diff.bisect import Bisector, BisectOutcome, Side
from reconkit.diff.compare import ColumnRule, compare_rows, sort_key
from reconkit.diff.model import DiffOptions, DiffResult, SideStats, Tolerance
from reconkit.diff.plan import Plan
from reconkit.diff.resolve import ColumnPair, resolve_columns
from reconkit.diff.sql import SideSql, join_diff_sql
from reconkit.errors import DiffError
from reconkit.types import Canon, ColumnInfo, Kind, TableRef


@dataclass
class TableSpec:
    """One side of a comparison."""

    adapter: Adapter
    table: str
    #: SQL predicate restricting the rows compared, in the table's own dialect.
    where: str | None = None
    #: Time zone that this table's timezone-naive timestamps are in. They are converted
    #: to UTC before comparison. Leave unset when naive timestamps are already UTC.
    naive_timezone: str | None = None


def _side(
    spec: TableSpec,
    pairs_key: Sequence[ColumnPair],
    pairs_val: Sequence[ColumnPair],
    is_left: bool,
    options: DiffOptions,
) -> Side:
    def pick(p: ColumnPair) -> tuple[ColumnInfo, Canon]:
        return (p.left if is_left else p.right, p.canon)

    sql = SideSql(
        spec.adapter.dialect,
        TableRef.parse(spec.table),
        [pick(p) for p in pairs_key],
        [pick(p) for p in pairs_val],
        where=spec.where,
        naive_timezone=spec.naive_timezone,
        sample=options.sample,
    )
    return Side(spec.adapter, sql)


def _join(
    left: Side,
    right: Side,
    key_names: Sequence[str],
    rules: Sequence[ColumnRule],
    options: DiffOptions,
) -> BisectOutcome:
    """Same-connection fast path: one FULL OUTER JOIN returns only differing rows."""
    n = len(key_names) + len(rules)
    rows = left.adapter.query(join_diff_sql(left.sql, right.sql))
    lrows = [r[:n] for r in rows if r[2 * n] is not None]
    rrows = [r[n : 2 * n] for r in rows if r[2 * n + 1] is not None]
    left.rows_fetched, right.rows_fetched = len(lrows), len(rrows)
    left.row_count = int(left.adapter.query(left.sql.count_sql())[0][0])
    right.row_count = int(right.adapter.query(right.sql.count_sql())[0][0])
    found = compare_rows(lrows, rrows, key_names, rules)
    out = BisectOutcome("join", found.diffs, found.duplicate_keys, found.within_tolerance, levels=1)
    if options.limit is not None and len(out.diffs) > options.limit:
        out.diffs, out.truncated = out.diffs[: options.limit], True
    return out


def diff_tables(
    left: TableSpec,
    right: TableSpec,
    key: Sequence[str],
    *,
    columns: Sequence[str] | None = None,
    ignore: Sequence[str] = (),
    mapping: Mapping[str, str] | None = None,
    tolerances: Mapping[str, Tolerance] | None = None,
    precision: Mapping[str, int] | None = None,
    options: DiffOptions | None = None,
) -> DiffResult:
    """Compare two tables and return every row that is missing or different.

    Column names (``key``, ``columns``, ``ignore``, and the keys of ``mapping``,
    ``tolerances`` and ``precision``) are the left table's names.
    """
    options = options or DiffOptions()
    options.validate()
    started = time.perf_counter()
    before = (left.adapter.stats.snapshot(), right.adapter.stats.snapshot())
    same_connection = left.adapter is right.adapter

    key_pairs, value_pairs, warnings = resolve_columns(
        left.adapter.get_columns(TableRef.parse(left.table)),
        right.adapter.get_columns(TableRef.parse(right.table)),
        key,
        columns=columns,
        ignore=ignore,
        mapping=mapping,
        tolerances=tolerances,
        precision=precision,
        options=options,
    )
    lside = _side(left, key_pairs, value_pairs, True, options)
    rside = _side(right, key_pairs, value_pairs, False, options)
    key_names = [p.name for p in key_pairs]
    rules = [ColumnRule(p.name, p.canon, p.tolerance) for p in value_pairs]

    if options.validate_unique_key:
        for label, side in (("left", lside), ("right", rside)):
            dupes = int(side.adapter.query(side.sql.duplicate_keys_sql())[0][0])
            if dupes:
                raise DiffError(
                    f"key {key_names} is not unique in the {label} table: "
                    f"{dupes} key value(s) occur more than once"
                )

    strategy = options.strategy
    if strategy == "join" and not same_connection:
        raise DiffError("strategy 'join' needs both tables on the same connection")
    if strategy == "auto":
        strategy = "join" if same_connection else "bisect"

    if strategy == "join":
        outcome = _join(lside, rside, key_names, rules, options)
    else:
        bisector = Bisector(lside, rside, key_names, rules, options)
        first = key_pairs[0]
        integer_key = (
            first.canon.kind is Kind.INTEGER
            and first.left.type.kind is Kind.INTEGER
            and first.right.type.kind is Kind.INTEGER
        )
        bucketing = options.bucketing
        if bucketing == "range" and not integer_key:
            raise DiffError(
                f"bucketing 'range' needs an integer first key column; {first.name!r} is "
                f"{first.left.type.native} / {first.right.type.native}"
            )
        if bucketing == "auto":
            bucketing = "range" if integer_key else "hash"
        plan: Plan | None = bisector.range_plan() if bucketing == "range" else bisector.hash_plan()
        outcome = (
            BisectOutcome("bisect/range") if plan is None else bisector.run(plan)
        )  # plan is None only when both sides are empty

    after = (left.adapter.stats.since(before[0]), right.adapter.stats.since(before[1]))
    if same_connection:
        # One connection did all the work; count it once, under the left side.
        after = (after[0], QueryStats())
    outcome.diffs.sort(key=lambda d: sort_key(d.key))
    return DiffResult(
        key_columns=key_names,
        compared_columns=[r.name for r in rules],
        strategy=outcome.strategy,
        left=SideStats(
            left.table,
            left.adapter.name,
            lside.row_count,
            after[0].queries,
            after[0].rows,
            lside.rows_fetched,
        ),
        right=SideStats(
            right.table,
            right.adapter.name,
            rside.row_count,
            after[1].queries,
            after[1].rows,
            rside.rows_fetched,
        ),
        diffs=outcome.diffs,
        duplicate_keys=sorted(set(outcome.duplicate_keys), key=sort_key),
        within_tolerance=outcome.within_tolerance,
        warnings=warnings,
        sample_rate=options.sample,
        truncated=outcome.truncated,
        levels=outcome.levels,
        duration_seconds=time.perf_counter() - started,
    )
