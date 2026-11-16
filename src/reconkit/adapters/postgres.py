"""PostgreSQL dialect and adapter (psycopg 3). Integration-tested."""

from __future__ import annotations

from typing import Any

from reconkit.adapters.base import HASH_SUBSTR_START, DbApiAdapter, Dialect
from reconkit.errors import AdapterError
from reconkit.types import ColumnType, Kind


class PostgresDialect(Dialect):
    name = "postgres"
    text_type = "TEXT"
    placeholder = "%s"

    def md5_int(self, expr: str) -> str:
        return f"('x' || SUBSTR(MD5({expr}), {HASH_SUBSTR_START}))::bit(60)::bigint"

    def decimal_text(self, expr: str, scale: int) -> str:
        return f"CAST(CAST({expr} AS NUMERIC(38, {scale})) AS TEXT)"

    def float_input(self, expr: str) -> str:
        # Postgres converts float4 to numeric through 6 significant digits (1048576 becomes
        # 1048580). Going through float8 first keeps 15, which matches other engines for
        # any value that fits float_precision.
        return f"CAST({expr} AS DOUBLE PRECISION)"

    def parse_type(self, native: str, precision: int | None, scale: int | None) -> ColumnType:
        base = native.strip().lower()
        if base == "numeric":
            # information_schema reports NULL precision and scale for unconstrained numeric.
            return ColumnType(
                Kind.DECIMAL, scale=scale if precision is not None else None, native=native
            )
        return super().parse_type(native, precision, scale)


def connect(config: dict[str, Any], name: str = "postgres") -> DbApiAdapter:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise AdapterError("install reconkit[postgres] to connect to PostgreSQL") from exc
    cfg = dict(config)
    url = cfg.pop("url", None)
    if "database" in cfg:
        cfg["dbname"] = cfg.pop("database")
    try:
        conn = (
            psycopg.connect(url, autocommit=True, **cfg)
            if url
            else psycopg.connect(autocommit=True, **cfg)
        )
    except Exception as exc:
        raise AdapterError(f"[{name}] cannot connect to PostgreSQL: {exc}") from exc
    return DbApiAdapter(PostgresDialect(), conn, name=name)
