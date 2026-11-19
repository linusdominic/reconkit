"""The Redshift adapter against Floci's Redshift emulation, paired with DuckDB and Postgres.

Floci (https://github.com/floci-io/floci) emulates the Redshift control plane: the test
creates a cluster through the Redshift API with boto3, reads its endpoint from
DescribeClusters and connects with ``redshift_connector``, as against a real cluster.
The data plane is a stock PostgreSQL container behind Floci's auth proxy, so SQL runs
with PostgreSQL semantics, not Redshift's. See docs/adapters.md.

PostgreSQL has no ``STRTOL``, which the dialect uses to turn the hex digest into an
integer, so the test installs a SQL function of that name (base 16 only) in the cluster
database. Everything else the adapter sends runs unchanged.

Start Floci with ``docker compose --profile emulators up -d floci``. Skipped when it is
not reachable, unless RECONKIT_REQUIRE_REDSHIFT=1, in which case that is a failure.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

from reconkit import TableSpec
from reconkit.adapters import DbApiAdapter
from reconkit.adapters.redshift import connect
from reconkit.types import Kind, TableRef
from tests.conftest import unavailable
from tests.integration import planted

pytestmark = pytest.mark.redshift
REQUIRE = "RECONKIT_REQUIRE_REDSHIFT"
ENDPOINT = os.environ.get("RECONKIT_TEST_FLOCI_ENDPOINT", "http://127.0.0.1:45670")
CLUSTER = "reconkit-test"
# Dummy credentials for a local emulator; nothing here reaches AWS.
USER, PASSWORD = "reconkit", "Reconkit-test-1"
DDL = (
    "id BIGINT, amount DECIMAL(14,2), ratio DOUBLE PRECISION, note VARCHAR(256),"
    " created TIMESTAMPTZ, updated TIMESTAMP, day DATE, flag BOOLEAN"
)
#: Stand-in for Redshift's STRTOL(text, base); see the module docstring.
STRTOL_SHIM = """
CREATE OR REPLACE FUNCTION public.strtol(text, integer) RETURNS bigint AS $$
    SELECT CASE WHEN $2 = 16 THEN ('x' || lpad($1, 16, '0'))::bit(64)::bigint END
$$ LANGUAGE sql IMMUTABLE STRICT
"""


def _redshift_client() -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "redshift",
        endpoint_url=ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(connect_timeout=3, read_timeout=60, retries={"max_attempts": 1}),
    )


@pytest.fixture(scope="session")
def cluster() -> Iterator[dict[str, Any]]:
    """A Floci cluster for the session; returns the adapter connection config."""
    with pytest.MonkeyPatch.context() as mp:
        # Keep botocore away from any real AWS configuration on the machine.
        mp.setenv("AWS_CONFIG_FILE", os.devnull)
        mp.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
        mp.setenv("AWS_EC2_METADATA_DISABLED", "true")
        try:
            client = _redshift_client()
            existing = {
                c["ClusterIdentifier"] for c in client.describe_clusters().get("Clusters", [])
            }
        except Exception as exc:
            unavailable(REQUIRE, f"Floci not reachable at {ENDPOINT}: {exc}")
        if CLUSTER not in existing:
            client.create_cluster(
                ClusterIdentifier=CLUSTER, NodeType="dc2.large", DBName="dev",
                MasterUsername=USER, MasterUserPassword=PASSWORD,
            )  # fmt: skip
        deadline = time.monotonic() + 180
        while True:
            info = client.describe_clusters(ClusterIdentifier=CLUSTER)["Clusters"][0]
            if info["ClusterStatus"] == "available" and info.get("Endpoint"):
                break
            if time.monotonic() > deadline:
                pytest.fail(f"Floci cluster {CLUSTER} not available: {info['ClusterStatus']}")
            time.sleep(1)
        config = {
            "host": info["Endpoint"]["Address"],
            "port": int(info["Endpoint"]["Port"]),
            "database": info.get("DBName", "dev"),
            "user": USER,
            "password": PASSWORD,
            "timeout": 30,
            # Floci's proxy presents a self-signed certificate that redshift_connector's
            # default sslmode (verify-ca) rejects; this is a local emulator.
            "ssl": False,
        }
        yield config
        client.delete_cluster(ClusterIdentifier=CLUSTER, SkipFinalClusterSnapshot=True)


@pytest.fixture
def rs(cluster: dict[str, Any]) -> Iterator[DbApiAdapter]:
    adapter = connect(cluster, name="redshift")
    adapter.execute(STRTOL_SHIM)
    adapter.execute("DROP SCHEMA IF EXISTS reconkit_rs CASCADE")
    adapter.execute("CREATE SCHEMA reconkit_rs")
    adapter.execute("SET search_path TO reconkit_rs, public")  # public holds the shim
    yield adapter
    adapter.execute("DROP SCHEMA IF EXISTS reconkit_rs CASCADE")
    adapter.close()


def seed(rs: DbApiAdapter, rows: list[planted.Row], table: str = "planted") -> TableSpec:
    # The DDL keywords are Redshift's; Floci's SQL interceptor strips them for PostgreSQL.
    rs.execute(f"CREATE TABLE {table} ({DDL}) DISTSTYLE KEY DISTKEY (id) SORTKEY (id)")
    if rows:
        rs.execute(planted.insert_sql(table, rows, planted.Syntax()))
    return TableSpec(rs, table)


def test_cluster_runs_postgres_not_redshift(rs: DbApiAdapter) -> None:
    """Pin what the emulator is, so the claims in docs/adapters.md stay true."""
    version = str(rs.query("SELECT version()")[0][0])
    assert version.startswith("PostgreSQL ")
    shimmed = rs.query("SELECT STRTOL(SUBSTRING(MD5('abc'), 18), 16)")[0][0]
    assert shimmed == int("900150983cd24fb0d6963f7d28e17f72"[-15:], 16)


def test_catalogue_types(rs: DbApiAdapter) -> None:
    seed(rs, [])
    columns = rs.get_columns(TableRef.parse("planted"))
    assert [(c.name, c.type.kind, c.type.scale, c.type.tz_aware) for c in columns] == [
        ("id", Kind.INTEGER, None, False),
        ("amount", Kind.DECIMAL, 2, False),
        ("ratio", Kind.FLOAT, None, False),
        ("note", Kind.TEXT, None, False),
        ("created", Kind.TIMESTAMP, None, True),
        ("updated", Kind.TIMESTAMP, None, False),
        ("day", Kind.DATE, None, False),
        ("flag", Kind.BOOLEAN, None, False),
    ]
    assert rs.get_columns(TableRef.parse("reconkit_rs.planted")) == columns


@pytest.mark.parametrize("bucketing", ["range", "hash"])
def test_planted_differences_are_found(
    rs: DbApiAdapter, reference: DbApiAdapter, bucketing: str
) -> None:
    left_rows, right_rows = planted.build()
    left = planted.seed_reference(reference, left_rows)
    right = seed(rs, right_rows)
    rs.execute("SET TIME ZONE 'America/New_York'")  # the session zone must not matter
    planted.assert_planted(planted.run(left, right, bucketing), bucketing)
    trimmed = planted.run(left, right, bucketing, trim_whitespace=True)
    assert planted.reported(trimmed) == planted.expected_trimmed()


def test_naive_timestamps_in_a_local_zone(rs: DbApiAdapter, reference: DbApiAdapter) -> None:
    rows, _ = planted.build()
    left = planted.seed_reference(reference, rows)
    rs.execute("CREATE TABLE local_times (id BIGINT, wall_clock TIMESTAMP)")
    values = ", ".join(
        f"({i}, {planted.literal(at, planted.Syntax())})" for i, at in planted.local_rows(rows)
    )
    rs.execute(f"INSERT INTO local_times VALUES {values}")
    planted.assert_naive_timezone(left, rs, "local_times")
