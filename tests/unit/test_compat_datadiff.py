"""``reconkit data-diff``: URIs, --conf files, column selection, ages and output formats."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from reconkit.cli import main
from reconkit.compat.datadiff import (
    DataDiffArgs,
    expand_columns,
    load_conf,
    parse_age,
    run_data_diff,
)
from reconkit.compat.uri import Connection, from_dict, from_uri
from reconkit.errors import ConfigError

# -- URIs -----------------------------------------------------------------------------------


def test_postgres_uri_is_passed_through() -> None:
    conn = from_uri("postgresql://u:p%40ss@db.internal:5433/app?sslmode=require")
    assert conn == Connection(
        "postgres", {"url": "postgresql://u:p%40ss@db.internal:5433/app?sslmode=require"}
    )
    assert from_uri("postgres://u@h/d").type == "postgres"


def test_redshift_uri() -> None:
    conn = from_uri("redshift://admin:s%2Fecret@cluster.example:5439/dev")
    assert conn.type == "redshift"
    assert conn.config == {
        "host": "cluster.example",
        "port": 5439,
        "user": "admin",
        "password": "s/ecret",
        "database": "dev",
    }
    assert from_uri("redshift://u:p@host/db").config["port"] == 5439


def test_snowflake_uri() -> None:
    conn = from_uri(
        "snowflake://me:pw@xy12345.eu-west-1/ANALYTICS/PUBLIC?warehouse=WH&role=R&key=/k.p8"
    )
    assert conn.config == {
        "account": "xy12345.eu-west-1",
        "user": "me",
        "password": "pw",
        "database": "ANALYTICS",
        "schema": "PUBLIC",
        "warehouse": "WH",
        "role": "R",
        "private_key_file": "/k.p8",
    }


def test_bigquery_uri_qualifies_bare_tables_with_the_dataset() -> None:
    conn = from_uri("bigquery://my-project/staging")
    assert conn.type == "bigquery"
    assert conn.config == {"project": "my-project"}
    assert conn.qualify("orders") == "staging.orders"
    assert conn.qualify("other.orders") == "other.orders"


@pytest.mark.parametrize(
    ("uri", "path"),
    [
        ("duckdb://main@/data/a.duckdb", "/data/a.duckdb"),
        ("duckdb:///data/a.duckdb", "/data/a.duckdb"),
        ("duckdb://local.duckdb", "local.duckdb"),
        ("duckdb://", ":memory:"),
    ],
)
def test_duckdb_uri(uri: str, path: str) -> None:
    assert from_uri(uri) == Connection("duckdb", {"path": path})


@pytest.mark.parametrize("uri", ["mysql://u:p@h/d", "trino://u@h/c/s", "databricks://:t@h/p"])
def test_unsupported_drivers_are_refused_by_name(uri: str) -> None:
    with pytest.raises(ConfigError, match="has no reconkit adapter"):
        from_uri(uri)


def test_not_a_uri() -> None:
    with pytest.raises(ConfigError, match="not a connection URI"):
        from_uri("localhost/db")


def test_conf_dictionaries() -> None:
    assert from_dict({"driver": "postgresql", "host": "h", "database": "d"}) == Connection(
        "postgres", {"host": "h", "database": "d"}
    )
    assert from_dict({"driver": "duckdb", "filepath": "x.duckdb"}) == Connection(
        "duckdb", {"path": "x.duckdb"}
    )
    bq = from_dict({"driver": "bigquery", "project": "p", "dataset": "ds"})
    assert bq.config == {"project": "p"} and bq.default_namespace == "ds"
    with pytest.raises(ConfigError, match="did not specify a driver"):
        from_dict({"host": "h"}, "warehouse")


# -- ages and columns -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "delta"),
    [
        ("5min", timedelta(minutes=5)),
        ("1h30min", timedelta(hours=1, minutes=30)),
        ("2days", timedelta(days=2)),
        ("1mon", timedelta(days=30)),
        ("1y", timedelta(days=365)),
        ("10s", timedelta(seconds=10)),
    ],
)
def test_parse_age(text: str, delta: timedelta) -> None:
    assert parse_age(text) == delta


@pytest.mark.parametrize("text", ["", "5", "5 min", "3fortnights", "1h1hours"])
def test_parse_age_rejects(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_age(text)


def test_expand_columns_with_wildcards_keeps_left_order() -> None:
    left = ["id", "amount_usd", "amount_eur", "Note", "only_left"]
    right = ["ID", "note", "AMOUNT_EUR", "amount_usd"]
    got = expand_columns(
        ["amount%", "note"], left, right, case_sensitive=False, left_label="a", right_label="b"
    )
    assert got == ["amount_usd", "amount_eur", "Note"]
    with pytest.raises(ConfigError, match="'only_left' not found in: b"):
        expand_columns(
            ["only_left"], left, right, case_sensitive=False, left_label="a", right_label="b"
        )
    with pytest.raises(ConfigError, match="not found"):
        expand_columns(["note"], left, right, case_sensitive=True, left_label="a", right_label="b")


# -- end to end on DuckDB files -------------------------------------------------------------


@pytest.fixture
def two_files(tmp_path: Path) -> tuple[str, str]:
    a, b = tmp_path / "a.duckdb", tmp_path / "b.duckdb"
    with duckdb.connect(str(a)) as con:
        con.execute(
            "CREATE TABLE loans (id INTEGER, status VARCHAR, amount DECIMAL(12,2), updated_at TIMESTAMP)"
        )
        con.execute(
            "INSERT INTO loans VALUES "
            "(1, 'open', 100.00, '2026-11-01 10:00:00'),"
            "(2, 'open', 200.00, '2026-11-01 10:00:00'),"
            "(3, 'closed', 300.00, '2026-11-01 10:00:00'),"
            "(4, 'open', 400.00, '2026-11-18 23:50:00')"
        )
    with duckdb.connect(str(b)) as con:
        con.execute(
            "CREATE TABLE loans (id INTEGER, status VARCHAR, amount DECIMAL(12,2), updated_at TIMESTAMP)"
        )
        con.execute(
            "INSERT INTO loans VALUES "
            "(1, 'open', 100.00, '2026-11-01 10:00:00'),"
            "(2, 'open', 250.00, '2026-11-01 10:00:00'),"  # amount changed
            "(4, 'open', 400.00, '2026-11-18 23:50:00'),"
            "(5, 'open', 500.00, '2026-11-01 10:00:00')"  # only in B; 3 only in A
        )
    return f"duckdb://{a}", f"duckdb://{b}"


def _cli(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(main, ["data-diff", *args])
    return result.exit_code, result.stdout, result.stderr


def test_default_compares_keys_only(two_files: tuple[str, str]) -> None:
    a, b = two_files
    code, out, _ = _cli(a, "loans", b, "loans")
    assert code == 0
    # data-diff compares only the key unless --columns or --update-column is given, so the
    # changed amount on id 2 is not reported.
    assert out.splitlines() == ["- 3", "+ 5"]


def test_columns_and_update_column(two_files: tuple[str, str]) -> None:
    a, b = two_files
    code, out, _ = _cli(a, "loans", b, "loans", "-c", "amount", "-t", "updated_at")
    assert code == 0
    assert out.splitlines() == [
        "- 2, 2026-11-01 10:00:00.000000, 200.00",
        "+ 2, 2026-11-01 10:00:00.000000, 250.00",
        "- 3, 2026-11-01 10:00:00.000000, 300.00",
        "+ 5, 2026-11-01 10:00:00.000000, 500.00",
    ]


def test_json_lines(two_files: tuple[str, str]) -> None:
    a, b = two_files
    _, out, _ = _cli(a, "loans", b, "loans", "-c", "%", "--json")
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[0] == ["-", ["2", "open", "200.00", "2026-11-01 10:00:00.000000"]]
    assert [sign for sign, _ in lines] == ["-", "+", "-", "+"]


def test_stats_text_and_json(two_files: tuple[str, str]) -> None:
    a, b = two_files
    _, out, _ = _cli(a, "loans", b, "loans", "-c", "amount", "--stats")
    head = out.split("\n\nExtra-Info:")[0].splitlines()
    assert head == [
        "4 rows in table A",
        "4 rows in table B",
        "1 rows exclusive to table A (not present in B)",
        "1 rows exclusive to table B (not present in A)",
        "1 rows updated",
        "2 rows unchanged",
        "50.00% difference score",
    ]
    _, out, _ = _cli(a, "loans", b, "loans", "-c", "amount", "--stats", "--json")
    doc = json.loads(out)
    assert {
        k: doc[k]
        for k in ("rows_A", "rows_B", "exclusive_A", "exclusive_B", "updated", "unchanged", "total")
    } == {
        "rows_A": 4,
        "rows_B": 4,
        "exclusive_A": 1,
        "exclusive_B": 1,
        "updated": 1,
        "unchanged": 2,
        "total": 3,
    }


def test_limit_counts_output_lines(two_files: tuple[str, str]) -> None:
    a, b = two_files
    _, out, _ = _cli(a, "loans", b, "loans", "-c", "amount", "-l", "3")
    assert len(out.splitlines()) == 3


def test_where_and_fail_on_diff(two_files: tuple[str, str]) -> None:
    a, b = two_files
    code, out, _ = _cli(a, "loans", b, "loans", "-w", "id IN (1, 4)", "-c", "%", "--fail-on-diff")
    assert (code, out) == (0, "")
    code, _, _ = _cli(a, "loans", b, "loans", "--fail-on-diff")
    assert code == 1


def test_ages_filter_on_the_update_column(two_files: tuple[str, str]) -> None:
    a, b = two_files
    conn_a, conn_b = from_uri(a), from_uri(b)
    now = datetime(2026, 11, 19, 0, 0, 0)
    base = {"database1": conn_a, "table1": "loans", "database2": conn_b, "table2": "loans"}
    # Only rows updated more than an hour ago: id 4 (updated at 23:50) is excluded.
    older = run_data_diff(DataDiffArgs(**base, update_column="updated_at", min_age="1h"), now=now)
    assert sorted((d.kind, d.key) for d in older.result.diffs) == [
        ("missing_in_left", ("5",)),
        ("missing_in_right", ("3",)),
    ]
    younger = run_data_diff(
        DataDiffArgs(**base, update_column="updated_at", max_age="1h", stats=True), now=now
    )
    assert (younger.rows_a, younger.rows_b, younger.result.diffs) == (1, 1, [])
    with pytest.raises(ConfigError, match="need --update-column"):
        run_data_diff(DataDiffArgs(**base, min_age="1h"), now=now)


def test_joindiff_needs_one_database(two_files: tuple[str, str]) -> None:
    a, b = two_files
    code, _, err = _cli(a, "loans", b, "loans", "-a", "joindiff")
    assert code == 2 and "same database" in err
    code, out, _ = _cli(a, "loans", a, "loans", "-a", "joindiff", "-c", "%")
    assert (code, out) == (0, "")


def test_errors_exit_non_zero(two_files: tuple[str, str]) -> None:
    a, b = two_files
    assert _cli(a, "loans", b)[0] == 2
    assert _cli(a, "missing", b, "loans")[0] in (2, 3)
    code, _, err = _cli(a, "loans", b, "loans", "-k", "nope")
    assert code == 2 and "'nope' not found" in err
    code, _, err = _cli(a, "loans", b, "loans", "--materialize-to-table", "x")
    assert code == 2 and "not supported" in err
    code, _, err = _cli(a, "loans", b, "loans", "--table-write-limit", "10")
    assert code == 2 and "not supported" in err
    code, _, err = _cli(a, "loans", b, "loans", "--stats", "-l", "1")
    assert code == 2
    assert _cli(a, "loans", b, "loans", "--select", "x")[0] == 2


def test_conf_file_runs(
    two_files: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = two_files
    monkeypatch.setenv("SIDE_B", b.removeprefix("duckdb://"))
    conf = tmp_path / "datadiff.toml"
    conf.write_text(
        f"""
[database.left]
driver = "duckdb"
filepath = "{a.removeprefix("duckdb://")}"

[database.right]
driver = "duckdb"
filepath = "${{SIDE_B}}"

[run.default]
key_columns = ["id"]
columns = ["amount"]

[run.loans]
1.database = "left"
1.table = "loans"
2.database = "right"
2.table = "loans"

[run.uri]
1.database = "{a}"
1.table = "loans"
2.database = "{b}"
2.table = "loans"
columns = []
""",
        encoding="utf-8",
    )
    code, out, _ = _cli("--conf", str(conf), "--run", "loans")
    assert code == 0
    assert out.splitlines() == ["- 2, 200.00", "+ 2, 250.00", "- 3, 300.00", "+ 5, 500.00"]
    # Command-line values win over the file.
    _, out, _ = _cli("--conf", str(conf), "--run", "loans", "-c", "status")
    assert out.splitlines() == ["- 3, closed", "+ 5, open"]
    _, out, _ = _cli("--conf", str(conf), "--run", "uri")
    assert out.splitlines() == ["- 3", "+ 5"]
    assert load_conf(conf, "loans")["table1"] == "loans"
    with pytest.raises(ConfigError, match="cannot find run"):
        load_conf(conf, "nope")


def test_conf_rejects_unknown_options(tmp_path: Path) -> None:
    conf = tmp_path / "bad.toml"
    conf.write_text('[run.default]\nkey_column = ["id"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown option"):
        load_conf(conf, None)


def test_version_flag() -> None:
    code, out, _ = _cli("--version")
    assert code == 0 and out.startswith("reconkit ")
