"""DuckDB dialect and adapter. Integration-tested."""

from __future__ import annotations

from typing import Any

from reconkit.adapters.base import HASH_SUBSTR_START, DbApiAdapter, Dialect
from reconkit.errors import AdapterError


class DuckDBDialect(Dialect):
    name = "duckdb"
    text_type = "VARCHAR"
    placeholder = "?"

    def md5_int(self, expr: str) -> str:
        return f"CAST('0x' || SUBSTR(MD5({expr}), {HASH_SUBSTR_START}) AS BIGINT)"

    def int_div(self, a: str, b: str) -> str:
        # '/' is floating-point division in DuckDB.
        return f"(({a}) // ({b}))"

    def date_text(self, expr: str) -> str:
        return f"STRFTIME({expr}, '%Y-%m-%d')"

    def format_timestamp(self, expr: str) -> str:
        return f"STRFTIME({expr}, '%Y-%m-%d %H:%M:%S.%f')"


def connect(config: dict[str, Any], name: str = "duckdb") -> DbApiAdapter:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise AdapterError("install reconkit[duckdb] to connect to DuckDB") from exc
    cfg = dict(config)
    path = str(cfg.pop("path", cfg.pop("database", ":memory:")))
    try:
        conn = duckdb.connect(path, read_only=bool(cfg.pop("read_only", False)))
    except Exception as exc:
        raise AdapterError(f"[{name}] cannot open DuckDB database {path!r}: {exc}") from exc
    return from_connection(conn, name=name)


def from_connection(connection: Any, name: str = "duckdb") -> DbApiAdapter:
    """Wrap an existing ``duckdb.DuckDBPyConnection``."""
    return DbApiAdapter(DuckDBDialect(), connection, name=name)
