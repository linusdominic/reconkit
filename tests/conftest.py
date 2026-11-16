from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import duckdb
import pytest

from reconkit.adapters import DbApiAdapter
from reconkit.adapters.duckdb import from_connection

PG_URL = os.environ.get(
    "RECONKIT_TEST_PG_URL", "postgresql://reconkit:reconkit@localhost:55439/reconkit"
)


def make_duck(name: str = "duck") -> DbApiAdapter:
    return from_connection(duckdb.connect(":memory:"), name=name)


@pytest.fixture
def duck() -> Iterator[Callable[..., DbApiAdapter]]:
    """Factory for independent in-memory DuckDB adapters, closed after the test."""
    made: list[DbApiAdapter] = []

    def factory(name: str = "duck") -> DbApiAdapter:
        adapter = make_duck(name)
        made.append(adapter)
        return adapter

    yield factory
    for adapter in made:
        adapter.close()


def load(adapter: DbApiAdapter, table: str, ddl: str, rows: list[tuple[Any, ...]]) -> None:
    """Create ``table`` with column DDL ``ddl`` and insert ``rows``."""
    adapter.execute(f"DROP TABLE IF EXISTS {table}")
    adapter.execute(f"CREATE TABLE {table} ({ddl})")
    if rows:
        marks = ", ".join([adapter.dialect.placeholder] * len(rows[0]))
        adapter.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)


@pytest.fixture(scope="session")
def pg_url() -> str:
    """URL of the docker-compose Postgres. Tests are skipped when it is not reachable,
    unless RECONKIT_REQUIRE_PG=1 (set in CI), in which case that is a failure."""
    psycopg = pytest.importorskip("psycopg")
    try:
        psycopg.connect(PG_URL, connect_timeout=3).close()
    except Exception as exc:
        if os.environ.get("RECONKIT_REQUIRE_PG") == "1":
            raise
        pytest.skip(f"Postgres not reachable at {PG_URL}: {exc}")
    return PG_URL


@pytest.fixture
def pg(pg_url: str) -> Iterator[DbApiAdapter]:
    """Postgres adapter in a throwaway schema that is dropped afterwards."""
    from reconkit.adapters.postgres import connect

    adapter = connect({"url": pg_url}, name="pg")
    adapter.execute("DROP SCHEMA IF EXISTS reconkit_test CASCADE")
    adapter.execute("CREATE SCHEMA reconkit_test")
    adapter.execute("SET search_path TO reconkit_test")
    yield adapter
    adapter.execute("DROP SCHEMA IF EXISTS reconkit_test CASCADE")
    adapter.close()
