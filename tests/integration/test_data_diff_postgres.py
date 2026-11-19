"""``reconkit data-diff`` across engines: a PostgreSQL URI against a DuckDB file."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

from reconkit.adapters.postgres import connect
from reconkit.cli import main

pytestmark = pytest.mark.postgres

SCHEMA = "reconkit_datadiff_test"
DDL = "loan_id bigint, status varchar(20), principal numeric(14,2), funded_at timestamp"
ROWS = [
    (1, "open", "1000.00", "2026-11-01 09:00:00"),
    (2, "open", "2000.00", "2026-11-01 09:00:00"),
    (3, "closed", "3000.50", "2026-11-02 09:00:00.123456"),
    (4, "open", "4000.00", "2026-11-03 09:00:00"),
    (6, "open", None, "2026-11-03 09:00:00"),
]


@pytest.fixture
def sides(pg_url: str, tmp_path: Path) -> Iterator[tuple[str, str]]:
    pg = connect({"url": pg_url}, name="pg")
    pg.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    pg.execute(f"CREATE SCHEMA {SCHEMA}")
    pg.execute(f"CREATE TABLE {SCHEMA}.loans ({DDL})")
    pg.executemany(f"INSERT INTO {SCHEMA}.loans VALUES (%s, %s, %s, %s)", ROWS)
    path = tmp_path / "warehouse.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute(f"CREATE TABLE loans ({DDL.replace('bigint', 'BIGINT')})")
        rows = [r for r in ROWS if r[0] != 4]  # 4 is missing from the copy
        rows = [(2, "open", "2000.01", r[3]) if r[0] == 2 else r for r in rows]  # one cent
        rows.append((5, "open", "5000.00", "2026-11-04 09:00:00"))  # only in the copy
        con.executemany("INSERT INTO loans VALUES (?, ?, ?, ?)", rows)
    try:
        yield pg_url, f"duckdb://{path}"
    finally:
        pg.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        pg.close()


def _run(*args: str) -> list[str]:
    result = CliRunner().invoke(main, ["data-diff", *args])
    assert result.exit_code == 0, result.output
    return result.stdout.splitlines()


def test_postgres_against_duckdb(sides: tuple[str, str]) -> None:
    pg, duck = sides
    a, b = f"{SCHEMA}.loans", "loans"
    assert _run(pg, a, duck, b, "-k", "loan_id") == ["- 4", "+ 5"]
    assert _run(pg, a, duck, b, "-k", "loan_id", "-c", "%") == [
        "- 2, open, 2000.00, 2026-11-01 09:00:00.000000",
        "+ 2, open, 2000.01, 2026-11-01 09:00:00.000000",
        "- 4, open, 4000.00, 2026-11-03 09:00:00.000000",
        "+ 5, open, 5000.00, 2026-11-04 09:00:00.000000",
    ]
    stats = json.loads(_run(pg, a, duck, b, "-k", "loan_id", "-c", "%", "--stats", "--json")[0])
    assert (stats["rows_A"], stats["rows_B"], stats["exclusive_A"], stats["exclusive_B"]) == (
        5,
        5,
        1,
        1,
    )
    assert (stats["updated"], stats["unchanged"]) == (1, 3)


@pytest.mark.parametrize("algorithm", ["hashdiff", "auto"])
def test_bisection_settings_do_not_change_the_answer(
    sides: tuple[str, str], algorithm: str
) -> None:
    pg, duck = sides
    lines = _run(
        pg,
        f"{SCHEMA}.loans",
        duck,
        "loans",
        "-k",
        "loan_id",
        "-c",
        "principal",
        "-a",
        algorithm,
        "--bisection-factor",
        "2",
        "--bisection-threshold",
        "1",
    )
    assert lines == ["- 2, 2000.00", "+ 2, 2000.01", "- 4, 4000.00", "+ 5, 5000.00"]


def test_joindiff_inside_postgres(sides: tuple[str, str]) -> None:
    pg, _ = sides
    lines = _run(
        pg, f"{SCHEMA}.loans", pg, f"{SCHEMA}.loans", "-a", "joindiff", "-k", "loan_id", "-c", "%"
    )
    assert lines == []
