"""The BigQuery adapter against goccy/bigquery-emulator, paired with DuckDB and Postgres.

The emulator (https://github.com/goccy/bigquery-emulator) serves the BigQuery REST API
and evaluates GoogleSQL with go-zetasql (Google's own GoogleSQL front end) over SQLite.
The adapter talks to it with the real ``google-cloud-bigquery`` client and its DB-API
layer, so the client path, the SQL grammar and most function semantics are exercised;
storage, typing details and the catalogue are the emulator's. See docs/adapters.md.

Start it with ``docker compose --profile emulators up -d bigquery``. Skipped when it is
not reachable, unless RECONKIT_REQUIRE_BIGQUERY=1, in which case that is a failure.
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from reconkit import TableSpec
from reconkit.adapters import DbApiAdapter
from reconkit.adapters.bigquery import BigQueryDialect, client_options, connect
from reconkit.errors import ConfigError
from reconkit.types import Kind, TableRef
from tests.conftest import unavailable
from tests.integration import planted

pytestmark = [
    pytest.mark.bigquery,
    # The client suggests google-cloud-bigquery-storage for faster reads; not needed here.
    pytest.mark.filterwarnings("ignore:Cannot create BigQuery Storage client:UserWarning"),
]
REQUIRE = "RECONKIT_REQUIRE_BIGQUERY"
ENDPOINT = os.environ.get("RECONKIT_TEST_BQ_ENDPOINT", "http://127.0.0.1:59050")
PROJECT = "reconkit-test"  # BIGQUERY_EMULATOR_PROJECT in docker-compose.yml
DATASET = "reconkit"  # BIGQUERY_EMULATOR_DATASET

DDL = (
    "id INT64, amount NUMERIC(14, 2), ratio FLOAT64, note STRING, created TIMESTAMP,"
    " updated DATETIME, day DATE, flag BOOL"
)


class EmulatorDialect(BigQueryDialect):
    """The BigQuery dialect with one fragment replaced to step around an emulator bug.

    bigquery-emulator 0.8.1 evaluates ``MOD`` on INT64 through floating point, so it is
    wrong above 2**53 (``MOD(9007199254740993, 2)`` returns 0). Hash bucketing takes the
    modulus of 60-bit hashes, so every row would land in the wrong bucket. ``DIV`` is
    exact in the emulator, and for the non-negative operands reconkit uses
    ``a - b * DIV(a, b)`` equals ``MOD(a, b)``. The production ``mod`` is not exercised.
    """

    def mod(self, a: str, b: str) -> str:
        return f"(({a}) - ({b}) * DIV({a}, {b}))"


SYNTAX = planted.Syntax(
    text=BigQueryDialect().literal,
    decimal=lambda d: f"NUMERIC '{d}'",
    aware=lambda s: f"TIMESTAMP '{s}'",
    naive=lambda s: f"DATETIME '{s}'",
)


@pytest.fixture(scope="session")
def bq_endpoint() -> str:
    url = f"{ENDPOINT}/bigquery/v2/projects/{PROJECT}/datasets/{DATASET}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            json.load(response)
    except Exception as exc:
        unavailable(REQUIRE, f"BigQuery emulator not reachable at {ENDPOINT}: {exc}")
    return ENDPOINT


@pytest.fixture
def bq(bq_endpoint: str) -> Iterator[DbApiAdapter]:
    adapter = connect(
        {"project": PROJECT, "api_endpoint": bq_endpoint, "anonymous": True}, name="bigquery"
    )
    adapter.dialect = EmulatorDialect()
    yield adapter
    while _created:
        adapter.execute(f"DROP TABLE IF EXISTS {_created.pop()}")
    adapter.close()


_created: list[str] = []


def table_name(stem: str) -> str:
    """A fresh table per test (the emulator is shared), dropped by the ``bq`` fixture."""
    name = f"{DATASET}.{stem}_{uuid.uuid4().hex[:8]}"
    _created.append(name)
    return name


def seed(bq: DbApiAdapter, rows: list[planted.Row]) -> TableSpec:
    table = table_name("planted")
    bq.execute(f"CREATE TABLE {table} ({DDL})")
    if rows:
        bq.execute(planted.insert_sql(table, rows, SYNTAX))
    return TableSpec(bq, table)


def test_catalogue_types(bq: DbApiAdapter) -> None:
    spec = seed(bq, [])
    columns = bq.get_columns(TableRef.parse(spec.table))
    assert [(c.name, c.type.kind, c.type.tz_aware) for c in columns] == [
        ("id", Kind.INTEGER, False),
        ("amount", Kind.DECIMAL, False),
        ("ratio", Kind.FLOAT, False),
        ("note", Kind.TEXT, False),
        ("created", Kind.TIMESTAMP, True),
        ("updated", Kind.TIMESTAMP, False),
        ("day", Kind.DATE, False),
        ("flag", Kind.BOOLEAN, False),
    ]
    # The emulator reports NUMERIC(14, 2) as plain NUMERIC (BigQuery reports the
    # parameters), so the scale falls back to NUMERIC's 9 here.
    assert columns[1].type.scale in (2, 9)
    # project.dataset.table resolves to the same catalogue. A project id with a hyphen
    # has to be backtick-quoted: TableRef does not accept it as a bare identifier.
    assert bq.get_columns(TableRef.parse(f"`{PROJECT}`.{spec.table}")) == columns


@pytest.mark.parametrize("bucketing", ["range", "hash"])
def test_planted_differences_are_found(
    bq: DbApiAdapter, reference: DbApiAdapter, bucketing: str
) -> None:
    left_rows, right_rows = planted.build()
    left = planted.seed_reference(reference, left_rows)
    right = seed(bq, right_rows)
    planted.assert_planted(planted.run(left, right, bucketing), bucketing)
    trimmed = planted.run(left, right, bucketing, trim_whitespace=True)
    assert planted.reported(trimmed) == planted.expected_trimmed()


def test_naive_timestamps_in_a_local_zone(bq: DbApiAdapter, reference: DbApiAdapter) -> None:
    rows, _ = planted.build()
    left = planted.seed_reference(reference, rows)
    table = table_name("local_times")
    bq.execute(f"CREATE TABLE {table} (id INT64, wall_clock DATETIME)")
    values = ", ".join(
        f"({i}, {planted.literal(at, SYNTAX)})" for i, at in planted.local_rows(rows)
    )
    bq.execute(f"INSERT INTO {table} (id, wall_clock) VALUES {values}")
    planted.assert_naive_timezone(left, bq, table)


def test_emulator_mod_defect_is_still_present(bq: DbApiAdapter) -> None:
    """Fails once the emulator fixes MOD, so the workaround above can be dropped."""
    assert bq.query("SELECT MOD(9007199254740993, 2), DIV(9007199254740993, 2)") == [
        (0, 4503599627370496)
    ]


def test_string_literals_round_trip(bq: DbApiAdapter) -> None:
    """GoogleSQL string escapes: quotes, backslashes and newlines in a literal."""
    value = 'it\'s "q" C:\\temp\nline 2 ☃'
    assert bq.query(f"SELECT {bq.dialect.literal(value)}") == [(value,)]


def test_connection_options(tmp_path: Path) -> None:
    from google.auth.credentials import AnonymousCredentials
    from google.oauth2 import service_account

    anonymous = client_options({"project": "p", "anonymous": True, "api_endpoint": ENDPOINT})
    assert isinstance(anonymous["credentials"], AnonymousCredentials)
    assert anonymous["client_options"] == {"api_endpoint": ENDPOINT}
    assert anonymous["project"] == "p"
    with pytest.raises(ConfigError, match="explicit 'project'"):
        client_options({"anonymous": True})
    with pytest.raises(ConfigError, match="not both"):
        client_options({"project": "p", "anonymous": True, "keyfile": "k.json"})
    with pytest.raises(ConfigError, match="cannot read keyfile"):
        client_options({"keyfile": str(tmp_path / "missing.json")})

    # A throwaway service-account key: parsed locally, never used to fetch a token.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    keyfile = tmp_path / "key.json"
    keyfile.write_text(json.dumps({
        "type": "service_account", "project_id": "from-keyfile", "private_key_id": "0",
        "private_key": pem.decode(), "client_email": "test@from-keyfile.invalid",
        "client_id": "0", "token_uri": "http://127.0.0.1:9/token",
    }))  # fmt: skip
    opts = client_options({"keyfile": str(keyfile), "location": "EU"})
    assert isinstance(opts["credentials"], service_account.Credentials)
    assert opts["credentials"].project_id == "from-keyfile"
    assert opts == {"credentials": opts["credentials"], "location": "EU"}
    adapter = connect({"keyfile": str(keyfile), "location": "EU"})
    try:
        client = adapter._connection._client
        assert client.project == "from-keyfile" and client.location == "EU"
    finally:
        adapter.close()
