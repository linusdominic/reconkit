from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any, NoReturn

import duckdb
import pytest

from reconkit.adapters import DbApiAdapter
from reconkit.adapters.duckdb import from_connection

PG_URL = os.environ.get(
    "RECONKIT_TEST_PG_URL", "postgresql://reconkit:reconkit@localhost:55439/reconkit"
)


#: Emulator-backed integration tests; each needs RECONKIT_REQUIRE_<ENGINE>=1 to fail
#: instead of skipping when its emulator is missing. See docs/adapters.md.
EMULATOR_MARKERS = {
    "snowflake": "runs the Snowflake adapter against fakesnow (in process, Python 3.11+)",
    "bigquery": "needs the bigquery-emulator container (docker compose --profile emulators)",
    "redshift": "needs the Floci container (docker compose --profile emulators)",
}


def pytest_configure(config: pytest.Config) -> None:
    for name, text in EMULATOR_MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {text}")


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


def unavailable(require_env: str, message: str) -> NoReturn:
    """Skip a test whose engine or emulator is not available, or fail it when
    ``require_env`` is set to 1 (as in CI), so that a broken setup cannot pass as skips."""
    if os.environ.get(require_env) == "1":
        pytest.fail(f"{message} ({require_env}=1)", pytrace=False)
    pytest.skip(message)


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


@pytest.fixture(params=["duckdb", "postgres"])
def reference(request: pytest.FixtureRequest) -> Iterator[DbApiAdapter]:
    """The other side of a cross-engine test: DuckDB, or the docker-compose Postgres."""
    if request.param == "postgres":
        yield request.getfixturevalue("pg")
        return
    adapter = make_duck("duckdb")
    yield adapter
    adapter.close()
