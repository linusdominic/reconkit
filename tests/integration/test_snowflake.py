"""The Snowflake adapter against fakesnow, paired with DuckDB and Postgres.

fakesnow (https://github.com/tekumara/fakesnow) patches ``snowflake.connector`` in
process: each statement is parsed as Snowflake SQL by sqlglot, rewritten for DuckDB and
run there. So these tests run the real adapter code path (``snowflake.connector.connect``,
DB-API cursors, the information_schema query, every SQL fragment the dialect renders)
through Snowflake's grammar, but the values come from DuckDB, not from Snowflake. See
docs/adapters.md for what that does and does not prove.

Skipped when fakesnow is not installed (it needs Python 3.11+), unless
RECONKIT_REQUIRE_SNOWFLAKE=1, in which case that is a failure.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from reconkit import TableSpec
from reconkit.adapters import DbApiAdapter
from reconkit.adapters.snowflake import SnowflakeDialect, connect
from reconkit.types import ColumnType, Kind, TableRef
from tests.conftest import unavailable
from tests.integration import planted

pytestmark = [
    pytest.mark.snowflake,
    # fakesnow's DB-API results go through Arrow; harmless, and noisy at this size.
    pytest.mark.filterwarnings("ignore:Arrow output on a DB-API result:UserWarning"),
]
REQUIRE = "RECONKIT_REQUIRE_SNOWFLAKE"
Duck = Callable[..., DbApiAdapter]

DDL = (
    "id NUMBER(38,0), amount NUMBER(14,2), ratio FLOAT, note VARCHAR, created TIMESTAMP_TZ,"
    " updated TIMESTAMP_NTZ, day DATE, flag BOOLEAN"
)
SYNTAX = planted.Syntax(
    text=SnowflakeDialect().literal,
    aware=lambda s: f"'{s}'::TIMESTAMP_TZ",
    naive=lambda s: f"'{s}'::TIMESTAMP_NTZ",
    day=lambda s: f"'{s}'::DATE",
)


class FakesnowDialect(SnowflakeDialect):
    """The Snowflake dialect with the one fragment fakesnow cannot run replaced.

    fakesnow 0.12.1 raises NotImplementedError for ``TO_NUMBER(<text>, '<format>')``,
    which is how the dialect turns the hex digest into an integer. The replacement
    computes the same 60-bit integer through DuckDB's ``'0x'`` cast. Everything else is
    the production dialect; the production ``md5_int`` is not exercised by these tests.
    """

    def md5_int(self, expr: str) -> str:
        return f"CAST(CAST('0x' || RIGHT(MD5({expr}), 15) AS BIGINT) AS NUMBER(38, 0))"


@pytest.fixture
def sf() -> Iterator[DbApiAdapter]:
    try:
        import fakesnow
    except ImportError as exc:
        unavailable(REQUIRE, f"fakesnow is not installed: {exc}")
    with fakesnow.patch():
        adapter = connect(
            {"account": "fake", "user": "fake", "password": "fake",
             "database": "RECONKIT", "schema": "TEST"},
            name="snowflake",
        )  # fmt: skip
        adapter.dialect = FakesnowDialect()
        yield adapter
        adapter.close()


def seed(sf: DbApiAdapter, rows: list[planted.Row], table: str = "planted") -> TableSpec:
    sf.execute(f"CREATE OR REPLACE TABLE {table} ({DDL})")
    if rows:
        sf.execute(planted.insert_sql(table, rows, SYNTAX))
    return TableSpec(sf, table)


def test_catalogue_types_and_case_folding(sf: DbApiAdapter) -> None:
    seed(sf, [])
    sf.execute('CREATE OR REPLACE TABLE "Mixed" ("Id" NUMBER(38,0), "Note" VARCHAR)')
    columns = sf.get_columns(TableRef.parse("planted"))
    assert [(c.name, c.type.kind, c.type.scale, c.type.tz_aware) for c in columns] == [
        ("ID", Kind.INTEGER, None, False),
        ("AMOUNT", Kind.DECIMAL, 2, False),
        ("RATIO", Kind.FLOAT, None, False),
        ("NOTE", Kind.TEXT, None, False),
        ("CREATED", Kind.TIMESTAMP, None, True),
        ("UPDATED", Kind.TIMESTAMP, None, False),
        ("DAY", Kind.DATE, None, False),
        ("FLAG", Kind.BOOLEAN, None, False),
    ]
    # Unquoted names fold to upper case; schema and database qualifiers are honoured.
    assert sf.get_columns(TableRef.parse("test.planted")) == columns
    assert sf.get_columns(TableRef.parse("reconkit.test.planted")) == columns
    mixed = sf.get_columns(TableRef.parse('"Mixed"'))
    assert [(c.name, c.type) for c in mixed] == [
        ("Id", ColumnType(Kind.INTEGER, native="NUMBER")),
        ("Note", ColumnType(Kind.TEXT, native="TEXT")),
    ]


@pytest.mark.parametrize("bucketing", ["range", "hash"])
def test_planted_differences_are_found(
    sf: DbApiAdapter, reference: DbApiAdapter, bucketing: str
) -> None:
    left_rows, right_rows = planted.build()
    left = planted.seed_reference(reference, left_rows)
    right = seed(sf, right_rows)
    planted.assert_planted(planted.run(left, right, bucketing), bucketing)
    trimmed = planted.run(left, right, bucketing, trim_whitespace=True)
    assert planted.reported(trimmed) == planted.expected_trimmed()


def test_identical_tables_match_with_snowflake_on_the_left(sf: DbApiAdapter, duck: Duck) -> None:
    rows, _ = planted.build()
    result = planted.run(seed(sf, rows), planted.seed_reference(duck(), rows), "hash")
    assert result.diffs == []
    assert result.left.row_count == result.right.row_count == planted.N_ROWS


def test_naive_timestamps_in_a_local_zone(sf: DbApiAdapter, reference: DbApiAdapter) -> None:
    rows, _ = planted.build()
    left = planted.seed_reference(reference, rows)
    sf.execute("CREATE OR REPLACE TABLE local_times (id NUMBER(38,0), wall_clock TIMESTAMP_NTZ)")
    values = ", ".join(
        f"({i}, {planted.literal(at, SYNTAX)})" for i, at in planted.local_rows(rows)
    )
    sf.execute(f"INSERT INTO local_times VALUES {values}")
    planted.assert_naive_timezone(left, sf, "local_times")
