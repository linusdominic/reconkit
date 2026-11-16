"""Golden SQL snapshots per dialect.

Snowflake, BigQuery and Redshift are not run against live accounts, so these snapshots
plus a sqlglot parse in the target grammar are the extent of their verification. To
update after an intentional change: RECONKIT_UPDATE_SNAPSHOTS=1 pytest tests/unit/test_dialects.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlglot

from reconkit.adapters.base import Dialect
from reconkit.adapters.bigquery import BigQueryDialect
from reconkit.adapters.duckdb import DuckDBDialect
from reconkit.adapters.postgres import PostgresDialect
from reconkit.adapters.redshift import RedshiftDialect
from reconkit.adapters.snowflake import SnowflakeDialect
from reconkit.diff.plan import HashPlan, RangePlan
from reconkit.diff.sql import SideSql, join_diff_sql
from reconkit.errors import ConfigError
from reconkit.types import Canon, ColumnInfo, ColumnType, Kind, TableRef

SNAPSHOTS = Path(__file__).parent.parent / "snapshots"
DIALECTS: dict[str, Dialect] = {
    "postgres": PostgresDialect(),
    "duckdb": DuckDBDialect(),
    "snowflake": SnowflakeDialect(),
    "bigquery": BigQueryDialect(),
    "redshift": RedshiftDialect(),
}


def col(name: str, kind: Kind, **kw: object) -> ColumnInfo:
    return ColumnInfo(name, ColumnType(kind, **kw))  # type: ignore[arg-type]


KEYS = [(col("loan_id", Kind.INTEGER), Canon(Kind.INTEGER))]
VALUES = [
    (col("amount", Kind.DECIMAL, scale=4), Canon(Kind.DECIMAL, scale=2)),
    (col("rate", Kind.FLOAT), Canon(Kind.DECIMAL, scale=6)),
    (col("status", Kind.TEXT), Canon(Kind.TEXT, trim=True, empty_as_null=True)),
    (col("ref", Kind.UUID), Canon(Kind.UUID)),
    (col("active", Kind.BOOLEAN), Canon(Kind.BOOLEAN)),
    (col("flag", Kind.BOOLEAN), Canon(Kind.INTEGER)),
    (col("opened", Kind.DATE), Canon(Kind.DATE)),
    (col("booked", Kind.DATE), Canon(Kind.TIMESTAMP)),
    (col("created_at", Kind.TIMESTAMP, tz_aware=True), Canon(Kind.TIMESTAMP)),
    (col("updated_at", Kind.TIMESTAMP), Canon(Kind.TIMESTAMP, ts_precision=3)),
]


def side(dialect: Dialect, *, sample: float | None = None) -> SideSql:
    return SideSql(
        dialect, TableRef.parse("analytics.loans"), KEYS, VALUES,
        where="status <> 'TEST'", naive_timezone="Asia/Singapore", sample=sample,
    )  # fmt: skip


def statements(dialect: Dialect) -> dict[str, str]:
    s = side(dialect)
    sampled = side(dialect, sample=0.05)
    range_plan = RangePlan.build(1, 1_000_000, factor=32, initial_buckets=64)
    hash_plan = HashPlan(factor=32, initial_buckets=64)
    return {
        "minmax": s.minmax_sql(),
        "count_sampled": sampled.count_sql(),
        "duplicate_keys": s.duplicate_keys_sql(),
        "range_checksum_level0": s.checksum_sql(range_plan, 0, None),
        "range_checksum_level1": s.checksum_sql(range_plan, 1, [0, 1, 7]),
        "range_fetch": s.fetch_sql(range_plan, 2, [10, 11, 40]),
        "hash_checksum_level0": s.checksum_sql(hash_plan, 0, None),
        "hash_checksum_level1_sampled": sampled.checksum_sql(hash_plan, 1, [3, 9]),
        "hash_fetch": s.fetch_sql(hash_plan, 1, [67, 131]),
        "join_diff": join_diff_sql(s, side(dialect)),
        "columns": dialect.columns_sql(TableRef.parse("analytics.loans")),
    }


def render(dialect: Dialect) -> str:
    return "".join(f"-- {name}\n{sql}\n\n" for name, sql in statements(dialect).items())


@pytest.mark.parametrize("name", sorted(DIALECTS))
def test_sql_matches_snapshot(name: str) -> None:
    path = SNAPSHOTS / f"{name}.sql"
    actual = render(DIALECTS[name])
    if os.environ.get("RECONKIT_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert path.exists(), f"missing snapshot {path}; run with RECONKIT_UPDATE_SNAPSHOTS=1"
    assert actual == path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(DIALECTS))
def test_sql_parses_in_target_grammar(name: str) -> None:
    for label, sql in statements(DIALECTS[name]).items():
        parsed = sqlglot.parse_one(sql, read=name)
        assert parsed is not None, label


def test_all_dialects_agree_on_statement_shape() -> None:
    """Same number of projected columns everywhere; only function spellings differ."""
    shapes = set()
    for name, dialect in DIALECTS.items():
        s = side(dialect)
        fetch = sqlglot.parse_one(s.fetch_sql(HashPlan(32, 64), 0, [1]), read=name)
        shapes.add(len(fetch.expressions))
    assert shapes == {len(KEYS) + len(VALUES)}


@pytest.mark.parametrize(
    ("dialect", "native", "precision", "scale", "expected"),
    [
        ("postgres", "numeric", 12, 2, ColumnType(Kind.DECIMAL, scale=2)),
        ("postgres", "numeric", None, None, ColumnType(Kind.DECIMAL, scale=None)),
        ("postgres", "timestamp with time zone", None, None, ColumnType(Kind.TIMESTAMP, tz_aware=True)),
        ("postgres", "timestamp without time zone", None, None, ColumnType(Kind.TIMESTAMP)),
        ("postgres", "character varying", None, None, ColumnType(Kind.TEXT)),
        ("postgres", "character", None, None, ColumnType(Kind.TEXT)),
        ("postgres", "double precision", 53, None, ColumnType(Kind.FLOAT)),
        ("postgres", "bigint", 64, 0, ColumnType(Kind.INTEGER)),
        ("postgres", "uuid", None, None, ColumnType(Kind.UUID)),
        ("postgres", "jsonb", None, None, ColumnType(Kind.OTHER)),
        ("duckdb", "DECIMAL(12,2)", 12, 2, ColumnType(Kind.DECIMAL, scale=2)),
        ("duckdb", "TIMESTAMP WITH TIME ZONE", None, None, ColumnType(Kind.TIMESTAMP, tz_aware=True)),
        ("duckdb", "TIMESTAMP_NS", None, None, ColumnType(Kind.TIMESTAMP)),
        ("duckdb", "HUGEINT", 128, 0, ColumnType(Kind.INTEGER)),
        ("duckdb", "DOUBLE", 53, 0, ColumnType(Kind.FLOAT)),
        ("duckdb", "BOOLEAN", None, None, ColumnType(Kind.BOOLEAN)),
        ("duckdb", "DATE", None, None, ColumnType(Kind.DATE)),
        ("snowflake", "NUMBER", 38, 0, ColumnType(Kind.INTEGER)),
        ("snowflake", "NUMBER", 14, 2, ColumnType(Kind.DECIMAL, scale=2)),
        ("snowflake", "TIMESTAMP_NTZ", None, None, ColumnType(Kind.TIMESTAMP)),
        ("snowflake", "TIMESTAMP_TZ", None, None, ColumnType(Kind.TIMESTAMP, tz_aware=True)),
        ("snowflake", "TIMESTAMP_LTZ", None, None, ColumnType(Kind.TIMESTAMP, tz_aware=True)),
        ("snowflake", "TEXT", None, None, ColumnType(Kind.TEXT)),
        ("snowflake", "FLOAT", None, None, ColumnType(Kind.FLOAT)),
        ("bigquery", "INT64", None, None, ColumnType(Kind.INTEGER)),
        ("bigquery", "NUMERIC", None, None, ColumnType(Kind.DECIMAL, scale=9)),
        ("bigquery", "NUMERIC(14, 2)", None, None, ColumnType(Kind.DECIMAL, scale=2)),
        ("bigquery", "BIGNUMERIC", None, None, ColumnType(Kind.DECIMAL, scale=38)),
        ("bigquery", "TIMESTAMP", None, None, ColumnType(Kind.TIMESTAMP, tz_aware=True)),
        ("bigquery", "DATETIME", None, None, ColumnType(Kind.TIMESTAMP)),
        ("bigquery", "STRING", None, None, ColumnType(Kind.TEXT)),
        ("bigquery", "FLOAT64", None, None, ColumnType(Kind.FLOAT)),
        ("bigquery", "BOOL", None, None, ColumnType(Kind.BOOLEAN)),
        ("redshift", "character varying", None, None, ColumnType(Kind.TEXT)),
        ("redshift", "numeric", 18, 4, ColumnType(Kind.DECIMAL, scale=4)),
        ("redshift", "timestamp with time zone", None, None, ColumnType(Kind.TIMESTAMP, tz_aware=True)),
    ],
)  # fmt: skip
def test_parse_type(dialect: str, native: str, precision: int | None, scale: int | None,
                    expected: ColumnType) -> None:  # fmt: skip
    got = DIALECTS[dialect].parse_type(native, precision, scale)
    assert (got.kind, got.scale, got.tz_aware) == (expected.kind, expected.scale, expected.tz_aware)
    assert got.native == native


def test_identifier_handling() -> None:
    ref = TableRef.parse('Analytics."Loans"')
    assert PostgresDialect().bare(ref.parts[0]) == "analytics"
    assert SnowflakeDialect().bare(ref.parts[0]) == "ANALYTICS"
    assert SnowflakeDialect().bare(ref.parts[1]) == "Loans"
    assert PostgresDialect().quote('we"ird') == '"we""ird"'
    assert PostgresDialect().literal("o'brien") == "'o''brien'"
    assert BigQueryDialect().table(TableRef.parse("proj.ds.t")) == "`proj.ds.t`"
    assert BigQueryDialect().quote("c") == "`c`"
    with pytest.raises(ConfigError, match=r"dataset\.table"):
        BigQueryDialect().columns_sql(TableRef.parse("t"))


def test_base_dialect_requires_md5_int() -> None:
    with pytest.raises(NotImplementedError):
        Dialect().md5_int("x")
