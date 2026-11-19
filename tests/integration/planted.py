"""A table pair with planted differences, shared by the emulator-backed integration tests.

The left table is the reference (DuckDB or Postgres), the right table lives in the engine
under test. Both are generated from the same Python values and written as SQL literals
in each engine's syntax, so the only differences are the ones planted below.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from reconkit import DiffOptions, DiffResult, TableSpec, diff_tables
from reconkit.adapters import DbApiAdapter, Dialect

N_ROWS = 400
COLUMNS = ("id", "amount", "ratio", "note", "created", "updated", "day", "flag")
UTC = timezone.utc
PLUS8 = timezone(timedelta(hours=8))

Row = dict[str, Any]


def base_row(i: int) -> Row:
    return {
        "id": i,
        "amount": Decimal(i * 7919 % 2_000_000 - 1_000_000) / 100,
        "ratio": i * 0.25 - 3,
        "note": f"row {i}",
        # Same instants, written with two different offsets.
        "created": (
            datetime(2024, 1, 1, tzinfo=UTC) + timedelta(seconds=i * 3601, microseconds=i)
        ).astimezone(PLUS8 if i % 2 else UTC),
        "updated": datetime(2024, 1, 1) + timedelta(minutes=i, microseconds=i * 10),
        "day": date(2024, 1, 1) + timedelta(days=i),
        "flag": i % 3 == 0,
    }


def _same(i: int, **values: Any) -> tuple[int, Row, Row]:
    row = {**base_row(i), **values}
    return i, row, dict(row)


#: Rows that are identical on both sides but exercise edge cases of the canonical form.
EDGE_ROWS = [
    _same(3, amount=None, ratio=None, note=None, created=None, updated=None, day=None, flag=None),
    _same(5, note='naïve ☃ | it\'s "quoted" C:\\temp\nsecond line'),
    _same(6, note=""),
    _same(7, amount=Decimal("-0.01"), ratio=-0.5),
    _same(8, amount=Decimal("99999999999.99"), ratio=1048576.0),
    _same(9, created=datetime(2023, 12, 31, 23, 59, 59, tzinfo=PLUS8)),
    _same(10, created=datetime(2024, 2, 29, 0, 0, 0, 1, tzinfo=UTC), day=date(2024, 2, 29)),
    _same(11, note="  leading and trailing  "),
    _same(12, note="日本語テキスト"),
]


def build() -> tuple[list[Row], list[Row]]:
    """(left rows, right rows) with the planted differences applied to the right."""
    left = {i: base_row(i) for i in range(1, N_ROWS + 1)}
    for i, lrow, _ in EDGE_ROWS:
        left[i] = lrow
    right = {i: dict(row) for i, row in left.items()}

    del right[17]  # missing in right
    right[N_ROWS + 1] = base_row(N_ROWS + 1)  # missing in left
    right[42]["amount"] = left[42]["amount"] + Decimal("0.01")
    right[77]["note"] = None  # value -> NULL
    left[79]["amount"] = None  # NULL -> value
    right[79]["amount"] = Decimal("0.00")
    right[100]["created"] = left[100]["created"] + timedelta(microseconds=1)
    right[120]["updated"] = left[120]["updated"] + timedelta(hours=1)
    right[150]["note"] = left[150]["note"] + " "  # trailing whitespace
    left[160]["note"] = "café"  # NFC
    right[160]["note"] = "café"  # NFD: same glyphs, different bytes
    right[200]["day"] = left[200]["day"] + timedelta(days=1)
    right[250]["flag"] = not left[250]["flag"]
    right[300]["ratio"] = left[300]["ratio"] + 0.25
    return [left[i] for i in sorted(left)], [right[i] for i in sorted(right)]


#: What reconkit must report (default options, no trimming), in report order.
EXPECTED = [
    ("missing_in_right", "17", []),
    ("changed", "42", [("amount", "-6674.02", "-6674.01")]),
    ("changed", "77", [("note", "row 77", None)]),
    ("changed", "79", [("amount", None, "0.00")]),
    ("changed", "100", [("created", "2024-01-05 04:01:40.000100", "2024-01-05 04:01:40.000101")]),
    ("changed", "120", [("updated", "2024-01-01 02:00:00.001200", "2024-01-01 03:00:00.001200")]),
    ("changed", "150", [("note", "row 150", "row 150 ")]),
    ("changed", "160", [("note", "café", "café")]),
    ("changed", "200", [("day", "2024-07-19", "2024-07-20")]),
    ("changed", "250", [("flag", "false", "true")]),
    ("changed", "300", [("ratio", "72.000000", "72.250000")]),
    ("missing_in_left", "401", []),
]

#: Small buckets so that every run goes through more than one bisection level.
OPTIONS = {"bisection_factor": 4, "bisection_threshold": 8, "initial_buckets": 4}


@dataclass(frozen=True)
class Syntax:
    """How one engine spells typed literals. Strings use the dialect's own ``literal``."""

    text: Callable[[str], str] = Dialect().literal
    decimal: Callable[[Decimal], str] = str
    aware: Callable[[str], str] = lambda s: f"TIMESTAMPTZ '{s}'"
    naive: Callable[[str], str] = lambda s: f"TIMESTAMP '{s}'"
    day: Callable[[str], str] = lambda s: f"DATE '{s}'"
    true: str = "true"
    false: str = "false"


def literal(value: Any, syntax: Syntax) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return syntax.true if value else syntax.false
    if isinstance(value, Decimal):
        return syntax.decimal(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return syntax.naive(value.isoformat(sep=" "))
        return syntax.aware(value.isoformat(sep=" "))
    if isinstance(value, date):
        return syntax.day(value.isoformat())
    return syntax.text(str(value))


def insert_sql(table: str, rows: list[Row], syntax: Syntax) -> str:
    values = ", ".join(
        "(" + ", ".join(literal(row[c], syntax) for c in COLUMNS) + ")" for row in rows
    )
    return f"INSERT INTO {table} ({', '.join(COLUMNS)}) VALUES {values}"


DUCK_DDL = (
    "id BIGINT, amount DECIMAL(14,2), ratio DOUBLE, note VARCHAR, created TIMESTAMPTZ,"
    " updated TIMESTAMP, day DATE, flag BOOLEAN"
)
PG_DDL = (
    "id bigint PRIMARY KEY, amount numeric(14,2), ratio double precision, note text,"
    " created timestamptz, updated timestamp, day date, flag boolean"
)


def seed_reference(adapter: DbApiAdapter, rows: list[Row], table: str = "planted") -> TableSpec:
    """Create and fill the reference table in DuckDB or Postgres."""
    ddl = DUCK_DDL if adapter.dialect.name == "duckdb" else PG_DDL
    adapter.execute(f"DROP TABLE IF EXISTS {table}")
    adapter.execute(f"CREATE TABLE {table} ({ddl})")
    adapter.execute(insert_sql(table, rows, Syntax()))
    return TableSpec(adapter, table)


def reported(result: DiffResult) -> list[tuple[str, str, list[tuple[str, str | None, str | None]]]]:
    return [
        (d.kind, d.key[0], [(c.column.lower(), c.left, c.right) for c in d.columns])
        for d in result.diffs
    ]


def run(left: TableSpec, right: TableSpec, bucketing: str, **options: Any) -> DiffResult:
    opts = DiffOptions(bucketing=bucketing, **{**OPTIONS, **options})  # type: ignore[arg-type]
    return diff_tables(left, right, ["id"], options=opts)


def assert_planted(result: DiffResult, bucketing: str) -> None:
    assert reported(result) == EXPECTED
    assert result.strategy == f"bisect/{bucketing}"
    assert result.levels > 1
    assert result.left.row_count == N_ROWS and result.right.row_count == N_ROWS
    # Only the mismatching buckets are downloaded, never the whole table.
    assert result.left.rows_fetched < N_ROWS // 2
    assert result.right.rows_fetched < N_ROWS // 2
    assert result.warnings == []


def expected_trimmed() -> list[tuple[str, str, list[tuple[str, str | None, str | None]]]]:
    """EXPECTED with ``trim_whitespace``: the trailing-space change is no longer one."""
    return [e for e in EXPECTED if e[1] != "150"]


SINGAPORE = "Asia/Singapore"


def local_rows(rows: list[Row]) -> list[tuple[int, datetime | None]]:
    """(id, naive Singapore wall-clock time of ``created``) for a ``naive_timezone`` test."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(SINGAPORE)
    return [
        (
            r["id"],
            None if r["created"] is None else r["created"].astimezone(zone).replace(tzinfo=None),
        )
        for r in rows
    ]


def assert_naive_timezone(left: TableSpec, right_adapter: DbApiAdapter, right_table: str) -> None:
    """``created`` (an instant) on the left equals naive Singapore time on the right."""
    shifted = TableSpec(right_adapter, right_table, naive_timezone=SINGAPORE)
    opts = DiffOptions(bucketing="range", **OPTIONS)  # type: ignore[arg-type]
    result = diff_tables(left, shifted, ["id"], columns=["created"], mapping={"created": "wall_clock"},
                         options=opts)  # fmt: skip
    assert result.diffs == []
    # Without the zone the eight-hour offset shows up on every non-NULL row.
    plain = diff_tables(left, TableSpec(right_adapter, right_table), ["id"], columns=["created"],
                        mapping={"created": "wall_clock"}, options=opts)  # fmt: skip
    assert len(plain.diffs) == result.left.row_count - 1
