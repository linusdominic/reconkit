"""Integration tests against a real PostgreSQL (docker-compose.yml), paired with DuckDB.

These are the tests that prove the cross-engine claim: the same logical value must
produce the same canonical text, and therefore the same checksum, in both engines.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from reconkit import DiffOptions, TableSpec, Tolerance, diff_tables
from reconkit.adapters import DbApiAdapter
from reconkit.adapters.postgres import connect
from reconkit.audit import write_audit
from reconkit.contracts import check_contract
from reconkit.contracts.model import parse_contract
from reconkit.errors import AdapterError
from reconkit.runner import run_config
from tests.conftest import load, make_duck
from tests.unit.test_property import OPTIONS, oracle, reported, rows, table_pair

pytestmark = pytest.mark.postgres
Duck = Callable[..., DbApiAdapter]

PG_DDL = """
    id bigint PRIMARY KEY, amount numeric(14,2), rate numeric(18,6), ratio real, free numeric,
    small smallint, created timestamptz, updated timestamp, day date, flag boolean,
    branch char(6), note text, ref uuid
"""
DUCK_DDL = """
    id INTEGER, amount DECIMAL(14,2), rate DOUBLE, ratio DOUBLE, free DECIMAL(18,6),
    small BIGINT, created TIMESTAMPTZ, updated TIMESTAMP, day DATE, flag BOOLEAN,
    branch VARCHAR, note VARCHAR, ref UUID
"""
#: The same logical rows written as SQL literals valid in both engines.
ROWS = [
    "(1, 1234.50, 0.045, 1.5, 10.25, 7, TIMESTAMPTZ '2024-03-10 01:30:00.123456+00',"
    " TIMESTAMP '2024-03-10 09:30:00', DATE '2024-03-10', true, 'SG01', 'plain',"
    " '123e4567-e89b-12d3-a456-426614174000')",
    "(2, -0.01, 12.999999, -0.25, 0, -3, TIMESTAMPTZ '2023-12-31 23:59:59+08',"
    " TIMESTAMP '2000-01-01 00:00:00.5', DATE '1999-12-31', false, 'SGHQ', 'naïve ☃ | it''s',"
    " 'ffffffff-ffff-ffff-ffff-ffffffffffff')",
    "(3, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
    "(4, 99999999999.99, 100, 1048576, 123456.789, 32767, TIMESTAMPTZ '2024-06-30 12:00:00-05',"
    " TIMESTAMP '2024-06-30 12:00:00', DATE '2024-02-29', true, 'SG114', '', NULL)",
]


def seed_pair(pg: DbApiAdapter, db: DbApiAdapter, n_extra: int = 0) -> tuple[TableSpec, TableSpec]:
    pg.execute(f"CREATE TABLE types ({PG_DDL})")
    db.execute(f"CREATE TABLE types ({DUCK_DDL})")
    for adapter in (pg, db):
        adapter.execute(f"INSERT INTO types VALUES {', '.join(ROWS)}")
    if n_extra:
        pg.execute(
            "INSERT INTO types (id, amount, rate, created, note) SELECT g, g * 1.01, g / 7.0, "
            "TIMESTAMPTZ '2024-01-01 00:00:00+00' + g * interval '1 second', md5(g::text) "
            f"FROM generate_series(10, {n_extra + 9}) g"
        )
        db.execute(
            "INSERT INTO types (id, amount, rate, created, note) SELECT g, g * 1.01, g / 7.0, "
            "TIMESTAMPTZ '2024-01-01 00:00:00+00' + to_seconds(g), md5(g::VARCHAR) "
            f"FROM range(10, {n_extra + 10}) t(g)"
        )
    return TableSpec(pg, "types"), TableSpec(db, "types")


@pytest.mark.parametrize("bucketing", ["range", "hash"])
def test_type_matrix_is_identical_across_engines(
    pg: DbApiAdapter, duck: Duck, bucketing: str
) -> None:
    left, right = seed_pair(pg, duck(), n_extra=5000)
    pg.execute("SET TIME ZONE 'America/New_York'")  # session zone must not affect the result
    opts = DiffOptions(bucketing=bucketing, trim_whitespace=True, bisection_factor=4,  # type: ignore[arg-type]
                       bisection_threshold=16, initial_buckets=8)  # fmt: skip
    result = diff_tables(left, right, ["id"], options=opts)
    assert result.diffs == []
    assert result.left.row_count == result.right.row_count == 5004
    assert result.left.rows_fetched == 0
    assert result.warnings == []


def test_differences_across_engines_are_reported_per_column(pg: DbApiAdapter, duck: Duck) -> None:
    left, right = seed_pair(pg, duck(), n_extra=2000)
    db = right.adapter
    db.execute("UPDATE types SET amount = amount + 0.01 WHERE id = 1")
    db.execute("UPDATE types SET created = created + INTERVAL 8 HOUR, flag = true WHERE id = 2")
    db.execute("UPDATE types SET rate = rate + 0.0000004 WHERE id = 4")  # below float_precision
    db.execute("UPDATE types SET note = 'x' WHERE id = 1500")
    db.execute("DELETE FROM types WHERE id = 777")
    pg.execute("DELETE FROM types WHERE id = 3")
    result = diff_tables(left, right, ["id"], options=DiffOptions(trim_whitespace=True))
    got = [
        (d.kind, d.key[0], [(c.column, c.left, c.right) for c in d.columns]) for d in result.diffs
    ]
    assert got == [
        ("changed", "1", [("amount", "1234.50", "1234.51")]),
        ("changed", "2", [
            ("created", "2023-12-31 15:59:59.000000", "2023-12-31 23:59:59.000000"),
            ("flag", "false", "true"),
        ]),
        ("missing_in_left", "3", []),
        ("missing_in_right", "777", []),
        ("changed", "1500", [("note", hashlib.md5(b"1500").hexdigest(), "x")]),
    ]  # fmt: skip
    assert result.left.rows_fetched < 2500


def test_char_padding_needs_trim_and_naive_timezone_is_applied(
    pg: DbApiAdapter, duck: Duck
) -> None:
    left, right = seed_pair(pg, duck())
    # Postgres already strips CHAR(n) padding when casting to text, so branch matches as is.
    assert diff_tables(left, right, ["id"], columns=["branch"]).is_match
    # 'updated' holds Singapore wall-clock time in DuckDB and UTC in Postgres' 'created'.
    mapped = diff_tables(left, TableSpec(right.adapter, "types", naive_timezone="Asia/Singapore"),
                         ["id"], columns=["created"], mapping={"created": "updated"},
                         options=DiffOptions(timestamp_precision=0))  # fmt: skip
    assert [d.key[0] for d in mapped.diffs] == ["2", "4"]
    assert mapped.diffs[0].columns[0].left == "2023-12-31 15:59:59"
    tolerant = diff_tables(left, right, ["id"], columns=["rate"], precision={"rate": 2},
                           tolerances={"rate": Tolerance(abs=Decimal("0.01"))})  # fmt: skip
    assert tolerant.is_match


def test_uuid_and_composite_text_keys(pg: DbApiAdapter, duck: Duck) -> None:
    db = duck()
    pg.execute(
        "CREATE TABLE apps (ref uuid, country text, amount numeric(10,2), PRIMARY KEY (ref, country))"
    )
    db.execute("CREATE TABLE apps (ref UUID, country VARCHAR, amount DECIMAL(10,2))")
    pg.execute(
        "INSERT INTO apps SELECT md5(g::text)::uuid, (ARRAY['SG','MY','é|'])[g % 3 + 1], g "
        "FROM generate_series(1, 3000) g"
    )
    db.execute(
        "INSERT INTO apps SELECT md5(g::VARCHAR)::UUID, ['SG','MY','é|'][g % 3 + 1], g "
        "FROM range(1, 3001) t(g)"
    )
    spec = (TableSpec(pg, "apps"), TableSpec(db, "apps"))
    opts = DiffOptions(bisection_factor=4, bisection_threshold=8, initial_buckets=4)
    clean = diff_tables(*spec, ["ref", "country"], options=opts)
    assert clean.strategy == "bisect/hash" and clean.is_match
    db.execute("DELETE FROM apps WHERE amount = 5")
    db.execute("UPDATE apps SET amount = 0 WHERE amount = 2999")
    result = diff_tables(*spec, ["ref", "country"], options=opts)
    assert sorted(d.kind for d in result.diffs) == ["changed", "missing_in_right"]
    assert result.levels > 2 and result.left.rows_fetched < 100


def test_same_connection_uses_join_and_two_connections_bisect(
    pg: DbApiAdapter, pg_url: str
) -> None:
    pg.execute(
        "CREATE TABLE a AS SELECT g AS id, g * 2 AS v, md5(g::text) AS t FROM generate_series(1, 5000) g"
    )
    pg.execute("CREATE TABLE b AS SELECT * FROM a WHERE id <> 10")
    pg.execute("UPDATE b SET v = NULL WHERE id = 4000")
    pg.execute("INSERT INTO b VALUES (NULL, 1, 'null key')")
    joined = diff_tables(
        TableSpec(pg, "reconkit_test.a"), TableSpec(pg, 'reconkit_test."b"'), ["id"]
    )
    assert joined.strategy == "join"
    assert [(d.kind, d.key) for d in joined.diffs] == [
        ("missing_in_left", (None,)),
        ("missing_in_right", ("10",)),
        ("changed", ("4000",)),
    ]
    assert joined.diffs[2].columns[0].right is None
    other = connect({"url": pg_url}, name="pg2")
    try:
        other.execute("SET search_path TO reconkit_test")
        bisected = diff_tables(TableSpec(pg, "a"), TableSpec(other, "b"), ["id"],
                               options=DiffOptions(bucketing="hash"))  # fmt: skip
        assert bisected.strategy == "bisect/hash"
        assert [(d.kind, d.key) for d in bisected.diffs] == [(d.kind, d.key) for d in joined.diffs]
    finally:
        other.close()


def test_contract_and_audit_on_postgres(pg: DbApiAdapter, duck: Duck) -> None:
    left, right = seed_pair(pg, duck())
    contract = parse_contract({
        "name": "types", "version": "1.0.0",
        "schema": [{"name": "types", "physicalName": "reconkit_test.types", "properties": [
            {"name": "id", "logicalType": "integer", "physicalType": "bigint", "primaryKey": True, "required": True},
            {"name": "amount", "logicalType": "number", "physicalType": "numeric(14,2)", "required": True},
            {"name": "created", "logicalType": "timestamp", "physicalType": "timestamp with time zone",
             "quality": [{"metric": "freshness", "unit": "days", "mustBeLessThan": 30}]},
            {"name": "flag", "logicalType": "boolean"},
            {"name": "branch", "logicalType": "string", "quality": [
                {"metric": "invalidValues", "arguments": {"validValues": ["SG01", "SGHQ"]}, "mustBe": 0}]},
            {"name": "ref", "logicalType": "string", "unique": True},
        ], "quality": [{"metric": "rowCount", "mustBe": 4}]}],
    })  # fmt: skip
    result = check_contract(pg, contract, now=datetime(2024, 7, 10, tzinfo=timezone.utc))
    failed = {(r.target, r.rule): r.observed for r in result.results if r.status != "pass"}
    assert set(failed) == {
        ("types.amount", "required"), ("types.branch", "invalidValues"),
        ("types.day", "declared in contract"), ("types.free", "declared in contract"),
        ("types.note", "declared in contract"), ("types.rate", "declared in contract"),
        ("types.ratio", "declared in contract"), ("types.small", "declared in contract"),
        ("types.updated", "declared in contract"),
    }  # fmt: skip
    assert failed[("types.branch", "invalidValues")] == "1"
    fresh = next(r for r in result.results if r.rule == "freshness")
    assert fresh.status == "pass" and "2024-06-30 17:00:00.000000 UTC" in fresh.observed

    right.adapter.execute("DELETE FROM types WHERE id = 1")
    right.adapter.execute("UPDATE types SET note = 'changed' WHERE id = 2")
    diff = diff_tables(left, right, ["id"])
    written = write_audit(pg, "reconkit_test.audit", run_id="run-1", check_name="types", result=diff,
                          recorded_at=datetime(2024, 7, 10, tzinfo=timezone.utc))  # fmt: skip
    assert written == 2
    write_audit(pg, "reconkit_test.audit", run_id="run-2", check_name="types", result=diff,
                recorded_at=datetime(2024, 7, 11, tzinfo=timezone.utc), max_rows=1)  # fmt: skip
    assert pg.query("SELECT run_id, status, missing_in_right, changed FROM audit_runs ORDER BY 1") == [
        ("run-1", "mismatch", 1, 1), ("run-2", "mismatch", 1, 1),
    ]  # fmt: skip
    assert pg.query("SELECT run_id, diff_kind, row_key, column_name, right_value FROM audit ORDER BY 1, 3") == [
        ("run-1", "missing_in_right", '{"id": "1"}', None, None),
        ("run-1", "changed", '{"id": "2"}', "note", "changed"),
        ("run-2", "missing_in_right", '{"id": "1"}', None, None),
    ]  # fmt: skip


def test_config_driven_run_against_postgres(pg: DbApiAdapter, tmp_path: Path, pg_url: str,
                                            monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    import duckdb

    pg.execute(
        "CREATE TABLE loans AS SELECT g AS loan_id, (g * 10)::numeric(12,2) AS amount FROM generate_series(1, 500) g"
    )
    with duckdb.connect(str(tmp_path / "wh.duckdb")) as wh:
        wh.execute(
            "CREATE TABLE loans AS SELECT g AS loan_id, (g * 10)::DECIMAL(12,2) AS amount FROM range(1, 501) t(g) WHERE g <> 250"
        )
    monkeypatch.setenv("RECON_TEST_PW", pg_url.split(":")[2].split("@")[0])
    config = {
        "connections": {
            "oltp": {"type": "postgres", "url": pg_url.replace(":reconkit@", ":${RECON_TEST_PW}@"),
                     "options": "-c search_path=reconkit_test"},
            "wh": {"type": "duckdb", "path": "wh.duckdb"},
        },
        "audit": {"connection": "oltp", "table": "reconkit_test.run_audit"},
        "checks": {"loans": {"left": {"connection": "oltp", "table": "loans"},
                             "right": {"connection": "wh", "table": "loans"}, "key": "loan_id"}},
    }  # fmt: skip
    (tmp_path / "reconkit.yml").write_text(yaml.safe_dump(config))
    report = run_config(tmp_path / "reconkit.yml")
    assert report.exit_code == 1
    assert report.outcomes[0].summary == "0 missing in left, 1 missing in right, 0 changed"
    assert pg.query("SELECT row_key FROM run_audit") == [('{"loan_id": "250"}',)]


def test_connection_failure_is_an_adapter_error() -> None:
    with pytest.raises(AdapterError, match="cannot connect"):
        connect({"host": "127.0.0.1", "port": 1, "user": "x", "dbname": "x", "connect_timeout": 1})


@settings(max_examples=25, deadline=None, suppress_health_check=list(HealthCheck))
@given(
    pair=table_pair(st.integers(-40, 400)),
    opts=OPTIONS,
    bucketing=st.sampled_from(["range", "hash"]),
)
def test_postgres_vs_duckdb_matches_oracle(pg: DbApiAdapter, pair: tuple[dict[Any, Any], dict[Any, Any]],
                                           opts: dict[str, int], bucketing: str) -> None:  # fmt: skip
    """The property test from tests/unit, with a real Postgres on the left side."""
    left, right = pair
    db = make_duck("right")
    try:
        ddl = "id INTEGER, amount DECIMAL(10,2), name VARCHAR, flag BOOLEAN, n INTEGER"
        load(pg, "prop", ddl, rows(left, 1))
        load(db, "prop", ddl, rows(right, 1))
        result = diff_tables(TableSpec(pg, "prop"), TableSpec(db, "prop"), ["id"],
                             options=DiffOptions(bucketing=bucketing, **opts))  # type: ignore[arg-type]  # fmt: skip
        assert reported(result) == oracle(left, right, 1)
    finally:
        db.close()
