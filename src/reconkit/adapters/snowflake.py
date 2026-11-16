"""Snowflake dialect and adapter.

Status: the SQL generation is unit-tested against golden snapshots and parsed with
sqlglot's Snowflake grammar. It has NOT been run against a live Snowflake account.
"""

from __future__ import annotations

from typing import Any

from reconkit.adapters.base import HASH_HEX_DIGITS, DbApiAdapter, Dialect
from reconkit.errors import AdapterError
from reconkit.types import ColumnType, Kind


class SnowflakeDialect(Dialect):
    name = "snowflake"
    text_type = "VARCHAR"
    placeholder = "%s"
    timestamp_format = "'YYYY-MM-DD HH24:MI:SS.FF6'"

    def fold(self, ident: str) -> str:
        return ident.upper()

    def md5_int(self, expr: str) -> str:
        return f"TO_NUMBER(RIGHT(MD5({expr}), {HASH_HEX_DIGITS}), '{'X' * HASH_HEX_DIGITS}')"

    def int_div(self, a: str, b: str) -> str:
        return f"FLOOR(({a}) / ({b}))"

    def mod(self, a: str, b: str) -> str:
        return f"MOD({a}, {b})"

    def decimal_text(self, expr: str, scale: int) -> str:
        return f"CAST(CAST({expr} AS NUMBER(38, {scale})) AS VARCHAR)"

    def aware_to_utc(self, expr: str) -> str:
        return f"CAST(CONVERT_TIMEZONE('UTC', {expr}) AS TIMESTAMP_NTZ)"

    def naive_to_utc(self, expr: str, zone: str) -> str:
        return f"CONVERT_TIMEZONE({self.literal(zone)}, 'UTC', {expr})"

    def date_to_timestamp(self, expr: str) -> str:
        return f"CAST({expr} AS TIMESTAMP_NTZ)"

    def default_schema_sql(self) -> str:
        return "CURRENT_SCHEMA()"

    def parse_type(self, native: str, precision: int | None, scale: int | None) -> ColumnType:
        base = native.strip().upper()
        if base == "NUMBER":
            if scale in (None, 0):
                return ColumnType(Kind.INTEGER, native=native)
            return ColumnType(Kind.DECIMAL, scale=scale, native=native)
        if base == "TIMESTAMP_NTZ":
            return ColumnType(Kind.TIMESTAMP, native=native)
        return super().parse_type(native, precision, scale)


def connect(config: dict[str, Any], name: str = "snowflake") -> DbApiAdapter:  # pragma: no cover
    try:
        import snowflake.connector
    except ImportError as exc:
        raise AdapterError("install reconkit[snowflake] to connect to Snowflake") from exc
    try:
        conn = snowflake.connector.connect(**config)
    except Exception as exc:
        raise AdapterError(f"[{name}] cannot connect to Snowflake: {exc}") from exc
    return DbApiAdapter(SnowflakeDialect(), conn, name=name)
