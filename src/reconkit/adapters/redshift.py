"""Amazon Redshift dialect and adapter.

Status: the SQL generation is unit-tested against golden snapshots and parsed with
sqlglot's Redshift grammar. The adapter is integration-tested against Floci 2.5.0's
Redshift emulation (tests/integration/test_redshift.py): a cluster created through the
Redshift API, reached with redshift_connector. Floci's data plane is PostgreSQL 16, so
that proves the connection path and PostgreSQL semantics only; ``STRTOL`` does not exist
there and the test supplies a stand-in. It has NOT been run against a live Redshift
cluster.
"""

from __future__ import annotations

from typing import Any

from reconkit.adapters.base import HASH_SUBSTR_START, DbApiAdapter, Dialect
from reconkit.errors import AdapterError


class RedshiftDialect(Dialect):
    name = "redshift"
    # A bare VARCHAR is VARCHAR(256) in Redshift and would truncate long text.
    text_type = "VARCHAR(65535)"
    placeholder = "%s"

    def md5_int(self, expr: str) -> str:
        return f"STRTOL(SUBSTRING(MD5({expr}), {HASH_SUBSTR_START}), 16)"

    def float_input(self, expr: str) -> str:
        # Same reasoning as the Postgres dialect. Verified on Floci (that is, on
        # PostgreSQL), not against a live cluster.
        return f"CAST({expr} AS DOUBLE PRECISION)"

    def sum_hash(self, expr: str) -> str:
        # SUM(BIGINT) returns BIGINT in Redshift and overflows on large tables.
        return f"SUM(CAST({expr} AS DECIMAL(38, 0)))"


def connect(config: dict[str, Any], name: str = "redshift") -> DbApiAdapter:
    try:
        import redshift_connector
    except ImportError as exc:
        raise AdapterError("install reconkit[redshift] to connect to Redshift") from exc
    try:
        conn = redshift_connector.connect(**config)
        conn.autocommit = True
    except Exception as exc:
        raise AdapterError(f"[{name}] cannot connect to Redshift: {exc}") from exc
    return DbApiAdapter(RedshiftDialect(), conn, name=name)
