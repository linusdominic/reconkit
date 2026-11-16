"""Diff behaviour, exercised against two separate in-memory DuckDB databases."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from reconkit import DiffOptions, DiffResult, TableSpec, Tolerance, diff_tables
from reconkit.adapters import DbApiAdapter
from reconkit.errors import DiffError
from tests.conftest import load

Duck = Callable[..., DbApiAdapter]
DDL = "id INTEGER, amount DECIMAL(12,2), name VARCHAR"
#: Small buckets everywhere, so even tiny tables go through several bisection levels.
DEEP = {"bisection_factor": 2, "bisection_threshold": 2, "initial_buckets": 2}


def base_rows(n: int = 200) -> list[tuple[Any, ...]]:
    return [(i, Decimal(i * 3) / 100, f"name-{i}") for i in range(1, n + 1)]


def two(duck: Duck, left: list[tuple[Any, ...]], right: list[tuple[Any, ...]], ddl: str = DDL,
        right_ddl: str | None = None) -> tuple[TableSpec, TableSpec]:  # fmt: skip
    a, b = duck("left"), duck("right")
    load(a, "t", ddl, left)
    load(b, "t", right_ddl or ddl, right)
    return TableSpec(a, "t"), TableSpec(b, "t")


def kinds(result: DiffResult) -> list[tuple[str, tuple[Any, ...]]]:
    return [(d.kind, d.key) for d in result.diffs]


@pytest.mark.parametrize("bucketing", ["range", "hash"])
@pytest.mark.parametrize("opts", [{}, DEEP, {**DEEP, "max_buckets_per_query": 1}])
def test_finds_missing_and_changed_rows(duck: Duck, bucketing: str, opts: dict[str, int]) -> None:
    left = base_rows()
    right = [r for r in left if r[0] not in (7, 150)]
    right[20] = (right[20][0], Decimal("999.99"), right[20][2])
    right[90] = (right[90][0], right[90][1], None)
    right.append((1000, Decimal("1.00"), "extra"))
    l, r = two(duck, left, right)
    result = diff_tables(l, r, ["id"], options=DiffOptions(bucketing=bucketing, **opts))  # type: ignore[arg-type]
    assert result.strategy == f"bisect/{bucketing}"
    assert kinds(result) == [
        ("missing_in_right", ("7",)),
        ("changed", (str(right[20][0]),)),
        ("changed", (str(right[90][0]),)),
        ("missing_in_right", ("150",)),
        ("missing_in_left", ("1000",)),
    ]
    assert result.diffs[1].columns[0].column == "amount"
    assert result.diffs[1].columns[0].right == "999.99"
    assert result.diffs[2].columns[0].right is None
    assert (result.left.row_count, result.right.row_count) == (200, 199)
    assert not result.is_match
    if opts:
        assert result.levels > 1


@pytest.mark.parametrize("bucketing", ["range", "hash"])
def test_identical_tables_download_nothing(duck: Duck, bucketing: str) -> None:
    l, r = two(duck, base_rows(), base_rows())
    result = diff_tables(l, r, ["id"], options=DiffOptions(bucketing=bucketing))  # type: ignore[arg-type]
    assert result.is_match
    assert result.left.rows_fetched == result.right.rows_fetched == 0
    assert result.levels == 1


def test_only_mismatching_buckets_are_downloaded(duck: Duck) -> None:
    left = [(i, Decimal(1), "x") for i in range(1, 10_001)]
    right = list(left)
    right[5000] = (5001, Decimal(2), "x")
    l, r = two(duck, left, right)
    result = diff_tables(
        l, r, ["id"],
        options=DiffOptions(bisection_factor=10, bisection_threshold=10, initial_buckets=10),
    )  # fmt: skip
    assert kinds(result) == [("changed", ("5001",))]
    assert result.left.rows_fetched <= 10
    assert result.levels == 3  # buckets of 1000 keys, then 100, then 10 (downloaded)


@pytest.mark.parametrize("bucketing", ["range", "hash"])
def test_empty_tables(duck: Duck, bucketing: str) -> None:
    opts = DiffOptions(bucketing=bucketing)  # type: ignore[arg-type]
    l, r = two(duck, [], [])
    assert diff_tables(l, r, ["id"], options=opts).is_match
    l, r = two(duck, base_rows(5), [])
    result = diff_tables(l, r, ["id"], options=opts)
    assert [k for k, _ in kinds(result)] == ["missing_in_right"] * 5
    l, r = two(duck, [], base_rows(5))
    assert diff_tables(l, r, ["id"], options=opts).missing_in_left == 5


def test_negative_and_sparse_integer_keys(duck: Duck) -> None:
    left = [(k, Decimal(1), "x") for k in (-(10**9), -5, 0, 3, 10**9, 2**31 - 1)]
    right = [(k, Decimal(1), "x") for k in (-(10**9), -4, 0, 3, 10**9)]
    l, r = two(duck, left, right)
    result = diff_tables(l, r, ["id"], options=DiffOptions(**DEEP))
    assert kinds(result) == [
        ("missing_in_right", ("-5",)),
        ("missing_in_left", ("-4",)),
        ("missing_in_right", (str(2**31 - 1),)),
    ]


def test_composite_key_with_integer_first_column_uses_range(duck: Duck) -> None:
    ddl = "loan_id BIGINT, seq INTEGER, amount DECIMAL(10,2)"
    left = [(loan, seq, Decimal(seq)) for loan in range(1, 30) for seq in range(1, 6)]
    right = [r for r in left if r[:2] != (7, 3)]
    right[0] = (1, 1, Decimal("5.55"))
    l, r = two(duck, left, right, ddl)
    result = diff_tables(l, r, ["loan_id", "seq"], options=DiffOptions(**DEEP))
    assert result.strategy == "bisect/range"
    assert kinds(result) == [("changed", ("1", "1")), ("missing_in_right", ("7", "3"))]


def test_text_and_uuid_keys_use_hash_bucketing(duck: Duck) -> None:
    ddl = "ref UUID, branch VARCHAR, amount DECIMAL(10,2)"
    left = [(f"00000000-0000-0000-0000-{i:012d}", f"B{i % 3}", Decimal(i)) for i in range(300)]
    right = [r for r in left if not r[0].endswith("0042")]
    right[10] = (right[10][0], right[10][1], Decimal("-1"))
    l, r = two(duck, left, right, ddl, "ref VARCHAR, branch VARCHAR, amount DECIMAL(10,2)")
    right_upper = [(ref.upper(), b, a) for ref, b, a in right]
    load(r.adapter, "t", "ref VARCHAR, branch VARCHAR, amount DECIMAL(10,2)", right_upper)
    result = diff_tables(l, r, ["ref", "branch"], options=DiffOptions(**DEEP))
    assert result.strategy == "bisect/hash"
    # uuid vs text compares case-insensitively; one row missing, one changed
    assert sorted(k for k, _ in kinds(result)) == ["changed", "missing_in_right"]
    with pytest.raises(DiffError, match="needs an integer first key"):
        diff_tables(l, r, ["ref"], options=DiffOptions(bucketing="range"))


def test_null_handling(duck: Duck) -> None:
    left = [(1, None, None), (2, Decimal(0), ""), (3, Decimal(1), "N"), (4, None, "a")]
    right = [(1, None, None), (2, None, None), (3, Decimal(1), None), (4, None, "a")]
    l, r = two(duck, left, right)
    result = diff_tables(l, r, ["id"])
    assert kinds(result) == [("changed", ("2",)), ("changed", ("3",))]
    assert [(c.column, c.left, c.right) for c in result.diffs[0].columns] == [
        ("amount", "0.00", None),
        ("name", "", None),
    ]
    # A literal 'N' must not collide with the NULL marker.
    assert [(c.left, c.right) for c in result.diffs[1].columns] == [("N", None)]
    relaxed = diff_tables(l, r, ["id"], options=DiffOptions(empty_string_as_null=True))
    assert [c.column for c in relaxed.diffs[0].columns] == ["amount"]


def test_null_keys(duck: Duck) -> None:
    left = [(None, Decimal(1), "a"), (1, Decimal(1), "b")]
    l, r = two(duck, left, [(1, Decimal(1), "b")])
    with pytest.raises(DiffError, match="NULL key"):
        diff_tables(l, r, ["id"])
    result = diff_tables(l, r, ["id"], options=DiffOptions(bucketing="hash"))
    assert kinds(result) == [("missing_in_right", (None,))]


def test_where_filter_mapping_ignore_and_columns(duck: Duck) -> None:
    a, b = duck("a"), duck("b")
    load(a, "src", "id INTEGER, status VARCHAR, amount DECIMAL(10,2), noise INTEGER, region VARCHAR",
         [(1, "OPEN", Decimal(1), 1, "sg"), (2, "OPEN", Decimal(2), 2, "sg"), (3, "OPEN", Decimal(3), 3, "my")])  # fmt: skip
    load(b, "dst", "ID INTEGER, loan_status VARCHAR, AMOUNT DECIMAL(10,2), noise INTEGER, region VARCHAR, loaded_at DATE",
         [(1, "OPEN", Decimal(1), 99, "sg", None), (2, "SHUT", Decimal(2), 98, "sg", None), (3, "X", Decimal(9), 97, "my", None)])  # fmt: skip
    left = TableSpec(a, "src", where="region = 'sg'")
    right = TableSpec(b, "dst", where="region = 'sg'")
    result = diff_tables(left, right, ["id"], mapping={"status": "loan_status"}, ignore=["noise"])
    assert kinds(result) == [("changed", ("2",))]
    assert result.diffs[0].columns[0].column == "status"
    assert result.compared_columns == ["status", "amount", "region"]
    assert any("loaded_at" in w for w in result.warnings)
    only_amount = diff_tables(left, right, ["id"], columns=["amount"])
    assert only_amount.is_match
    assert only_amount.compared_columns == ["amount"]


def test_numeric_normalisation_across_types(duck: Duck) -> None:
    left_ddl = "id INTEGER, a DECIMAL(18,4), b INTEGER, c DOUBLE, d BOOLEAN, e DECIMAL(10,2)"
    right_ddl = "id BIGINT, a DECIMAL(18,2), b DECIMAL(10,2), c DECIMAL(18,6), d INTEGER, e DOUBLE"
    left = [(1, Decimal("10.1249"), 5, 0.1 + 0.2, True, Decimal("7.10")),
            (2, Decimal("10.1250"), 5, 1.5, False, Decimal("7.10")),
            (3, Decimal("3.0000"), 5, 2.0, None, Decimal("7.10"))]  # fmt: skip
    right = [(1, Decimal("10.12"), Decimal("5.00"), Decimal("0.3"), 1, 7.1),
             (2, Decimal("10.13"), Decimal("5.50"), Decimal("1.5"), 0, 7.1000001),
             (3, Decimal("3.00"), Decimal("5.00"), Decimal("2.000001"), None, 7.11)]  # fmt: skip
    l, r = two(duck, left, right, left_ddl, right_ddl)
    result = diff_tables(l, r, ["id"])
    got = {(d.key[0], c.column): (c.left, c.right) for d in result.diffs for c in d.columns}
    assert got == {
        ("2", "b"): ("5.00", "5.50"),  # integer vs decimal keeps the decimal's scale
        ("3", "c"): ("2.000000", "2.000001"),  # floats compare at float_precision
        ("3", "e"): ("7.10", "7.11"),
    }
    coarse = diff_tables(l, r, ["id"], precision={"c": 3}, columns=["c"])
    assert coarse.is_match


def test_tolerances(duck: Duck) -> None:
    left = [(1, Decimal("100.00"), "a"), (2, Decimal("100.00"), "a"), (3, Decimal("1000.00"), "a")]
    right = [(1, Decimal("100.01"), "a"), (2, Decimal("100.50"), "a"), (3, Decimal("1000.90"), "a")]
    l, r = two(duck, left, right)
    assert diff_tables(l, r, ["id"]).changed == 3
    by_abs = diff_tables(l, r, ["id"], tolerances={"amount": Tolerance(abs=Decimal("0.01"))})
    assert kinds(by_abs) == [("changed", ("2",)), ("changed", ("3",))]
    assert by_abs.within_tolerance == 1
    by_rel = diff_tables(l, r, ["id"], tolerances={"AMOUNT": Tolerance(rel=Decimal("0.001"))})
    assert kinds(by_rel) == [("changed", ("2",))]
    assert by_rel.within_tolerance == 2


def test_timestamps_timezones_and_tolerance(duck: Duck) -> None:
    a, b = duck("a"), duck("b")
    a.execute("SET TimeZone = 'America/New_York'")  # session zone must not matter
    load(a, "t", "id INTEGER, ts TIMESTAMPTZ, day DATE", [])
    a.execute("""INSERT INTO t VALUES
        (1, TIMESTAMPTZ '2024-03-10 01:30:00+00', DATE '2024-03-10'),
        (2, TIMESTAMPTZ '2024-03-10 12:00:00.123456+00', DATE '2024-03-10'),
        (3, TIMESTAMPTZ '2024-03-10 12:00:00+00', DATE '2024-03-11')""")
    load(b, "t", "id INTEGER, ts TIMESTAMP, day TIMESTAMP", [])
    b.execute("""INSERT INTO t VALUES
        (1, TIMESTAMP '2024-03-10 09:30:00', TIMESTAMP '2024-03-10 00:00:00'),
        (2, TIMESTAMP '2024-03-10 20:00:00.123999', TIMESTAMP '2024-03-10 00:00:00'),
        (3, TIMESTAMP '2024-03-10 20:00:02', TIMESTAMP '2024-03-11 00:00:00')""")
    left = TableSpec(a, "t")
    # Right side stores naive Singapore local time (UTC+8).
    naive_utc = diff_tables(left, TableSpec(b, "t"), ["id"], columns=["ts"])
    assert naive_utc.changed == 3
    right = TableSpec(b, "t", naive_timezone="Asia/Singapore")
    exact = diff_tables(left, right, ["id"])
    assert {(d.key[0], c.column) for d in exact.diffs for c in d.columns} == {
        ("2", "ts"),
        ("3", "ts"),
    }
    assert exact.diffs[0].columns[0].left == "2024-03-10 12:00:00.123456"
    millis = diff_tables(left, right, ["id"], options=DiffOptions(timestamp_precision=3))
    assert kinds(millis) == [("changed", ("3",))]
    assert millis.diffs[0].columns[0].right == "2024-03-10 12:00:02.000"
    tolerant = diff_tables(left, right, ["id"], tolerances={"ts": Tolerance(seconds=2)})
    assert tolerant.is_match
    assert tolerant.within_tolerance == 2


def test_trim_whitespace(duck: Duck) -> None:
    left = [(1, Decimal(1), "SG01  "), (2, Decimal(1), "  x")]
    right = [(1, Decimal(1), "SG01"), (2, Decimal(1), "x")]
    l, r = two(duck, left, right)
    assert diff_tables(l, r, ["id"]).changed == 2
    trimmed = diff_tables(l, r, ["id"], options=DiffOptions(trim_whitespace=True))
    assert kinds(trimmed) == [("changed", ("2",))]  # only trailing whitespace is ignored


def test_text_with_separators_quotes_and_unicode(duck: Duck) -> None:
    ddl = "id INTEGER, a VARCHAR, b VARCHAR"
    rows = [(1, "a|b", "c"), (2, "it's", 'say "hi"'), (3, "naïve ☃", "日本語"), (4, "", " ")]
    l, r = two(duck, rows, rows, ddl)
    assert diff_tables(l, r, ["id"]).is_match
    changed = [*rows[:2], (3, "naive ☃", "日本語"), rows[3]]
    l, r = two(duck, rows, changed, ddl)
    assert kinds(diff_tables(l, r, ["id"])) == [("changed", ("3",))]


@pytest.mark.parametrize("bucketing", ["range", "hash"])
def test_sampling_is_deterministic_and_a_subset(duck: Duck, bucketing: str) -> None:
    left = [(i, Decimal(1), "x") for i in range(1, 5001)]
    right = [(i, Decimal(2 if i % 10 == 0 else 1), "x") for i in range(1, 5001)]
    l, r = two(duck, left, right)
    full = {d.key for d in diff_tables(l, r, ["id"]).diffs}
    opts = DiffOptions(sample=0.2, bucketing=bucketing)  # type: ignore[arg-type]
    first = diff_tables(l, r, ["id"], options=opts)
    second = diff_tables(l, r, ["id"], options=opts)
    sampled = {d.key for d in first.diffs}
    assert sampled == {d.key for d in second.diffs}
    assert sampled < full
    assert 0.1 < len(sampled) / len(full) < 0.3
    assert first.sample_rate == 0.2
    assert first.left.row_count is not None and 700 < first.left.row_count < 1300


def test_sampling_join_strategy_matches_bisect(duck: Duck) -> None:
    a = duck()
    load(a, "l", DDL, [(i, Decimal(1), "x") for i in range(1, 2001)])
    load(a, "r", DDL, [(i, Decimal(2 if i % 7 == 0 else 1), "x") for i in range(1, 2001)])
    opts = {"sample": 0.3}
    joined = diff_tables(TableSpec(a, "l"), TableSpec(a, "r"), ["id"], options=DiffOptions(**opts))
    bisected = diff_tables(TableSpec(a, "l"), TableSpec(a, "r"), ["id"],
                           options=DiffOptions(strategy="bisect", **opts))  # fmt: skip
    assert joined.strategy == "join"
    assert kinds(joined) == kinds(bisected) != []
    assert joined.left.row_count == bisected.left.row_count


def test_hash_bucketing_on_a_large_table_is_three_statements_per_side(duck: Duck) -> None:
    left = [(i, Decimal(1), "x") for i in range(1, 20_001)]
    right = [(i, Decimal(2 if i % 40 == 0 else 1), "x") for i in range(1, 20_001)]
    l, r = two(duck, left, right)
    l.adapter.sql_log = []  # type: ignore[attr-defined]
    result = diff_tables(l, r, ["id"], options=DiffOptions(bucketing="hash"))
    assert result.changed == 500
    statements = [q for q in l.adapter.sql_log if "information_schema" not in q]  # type: ignore[attr-defined]
    assert len(statements) == 3  # row count, checksum pass, one fetch of mismatching buckets
    assert result.levels == 1


def test_limit_stops_early(duck: Duck) -> None:
    left = base_rows(500)
    l, r = two(duck, left, [])
    result = diff_tables(
        l, r, ["id"], options=DiffOptions(limit=10, max_buckets_per_query=4, **DEEP)
    )
    assert len(result.diffs) == 10
    assert result.truncated
    assert result.left.rows_fetched < 500


def test_duplicate_keys(duck: Duck) -> None:
    left = [(1, Decimal(1), "a"), (1, Decimal(2), "b"), (2, Decimal(1), "c")]
    right = [(1, Decimal(1), "a"), (2, Decimal(1), "c")]
    l, r = two(duck, left, right)
    result = diff_tables(l, r, ["id"])
    assert result.duplicate_keys == [("1",)]
    assert not result.is_match
    with pytest.raises(DiffError, match="not unique in the left table: 1 key"):
        diff_tables(l, r, ["id"], options=DiffOptions(validate_unique_key=True))
    assert diff_tables(r, r, ["id"], options=DiffOptions(validate_unique_key=True)).is_match


def test_join_strategy_on_one_connection(duck: Duck) -> None:
    a = duck()
    left = base_rows(50)
    right = [r for r in left if r[0] != 5] + [
        (99, Decimal(1), "new"),
        (None, Decimal(1), "nullkey"),
    ]
    right[0] = (1, Decimal("0.03"), "renamed")
    load(a, "l", DDL, left)
    load(a, "r", DDL, right)
    result = diff_tables(TableSpec(a, "l"), TableSpec(a, "r"), ["id"],
                         tolerances={"amount": Tolerance(abs=Decimal("0.5"))})  # fmt: skip
    assert result.strategy == "join"
    assert kinds(result) == [
        ("missing_in_left", (None,)),
        ("changed", ("1",)),
        ("missing_in_right", ("5",)),
        ("missing_in_left", ("99",)),
    ]
    assert (result.left.row_count, result.right.row_count) == (50, 51)
    assert result.left.rows_fetched == 2  # only differing rows leave the database
    forced = diff_tables(TableSpec(a, "l"), TableSpec(a, "r"), ["id"],
                         options=DiffOptions(strategy="bisect", bucketing="hash"))  # fmt: skip
    assert kinds(forced) == kinds(result)
    limited = diff_tables(
        TableSpec(a, "l"), TableSpec(a, "r"), ["id"], options=DiffOptions(limit=2)
    )
    assert len(limited.diffs) == 2 and limited.truncated


def test_join_strategy_needs_one_connection(duck: Duck) -> None:
    l, r = two(duck, base_rows(3), base_rows(3))
    with pytest.raises(DiffError, match="same connection"):
        diff_tables(l, r, ["id"], options=DiffOptions(strategy="join"))


def test_missing_table_and_missing_key(duck: Duck) -> None:
    from reconkit.errors import AdapterError

    l, r = two(duck, base_rows(3), base_rows(3))
    with pytest.raises(AdapterError, match="not found"):
        diff_tables(l, TableSpec(r.adapter, "nope"), ["id"])
    with pytest.raises(DiffError, match="'missing' not found"):
        diff_tables(l, r, ["missing"])


def test_bisection_issues_one_query_per_side_per_level(duck: Duck) -> None:
    left = [(i, Decimal(1), "x") for i in range(1, 4097)]
    right = [(i, Decimal(2 if i in (10, 2000, 4000) else 1), "x") for i in range(1, 4097)]
    l, r = two(duck, left, right)
    l.adapter.sql_log = []  # type: ignore[attr-defined]
    result = diff_tables(
        l,
        r,
        ["id"],
        options=DiffOptions(bisection_factor=4, bisection_threshold=4, initial_buckets=4),
    )
    assert result.changed == 3
    log = l.adapter.sql_log  # type: ignore[attr-defined]
    checksum_queries = [q for q in log if "GROUP BY 1" in q]
    assert len(checksum_queries) == result.levels  # not one per mismatching bucket
    assert result.left.queries == len(log)
    assert result.to_dict(max_rows=1)["rows_listed"] == 1
    assert result.to_dict()["summary"]["changed_by_column"] == {"amount": 3}
