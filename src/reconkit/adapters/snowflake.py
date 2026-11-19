"""Snowflake dialect and adapter.

Status: the SQL generation is unit-tested against golden snapshots and parsed with
sqlglot's Snowflake grammar. The adapter is integration-tested against fakesnow 0.12.1
(tests/integration/test_snowflake.py), which runs ``snowflake.connector`` in process on
DuckDB, with snowflake-connector-python 4.x. That exercises the connector path, the
catalogue query and every fragment except ``md5_int`` (fakesnow cannot run
``TO_NUMBER`` with a format), but the values are DuckDB's. It has NOT been run against
a live Snowflake account.
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

    def literal(self, value: str) -> str:
        # Backslash starts an escape sequence in Snowflake string constants ('a\b' is
        # "a", backspace), so it has to be doubled along with the quote.
        return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"

    def md5_int(self, expr: str) -> str:
        return f"TO_NUMBER(RIGHT(MD5({expr}), {HASH_HEX_DIGITS}), '{'X' * HASH_HEX_DIGITS}')"

    def sum_hash(self, expr: str) -> str:
        # The hashes are NUMBER(38, 0) already, so the cast is a no-op in Snowflake. It
        # keeps the sum exact in fakesnow, which otherwise sums through DOUBLE.
        return f"SUM(CAST({expr} AS NUMBER(38, 0)))"

    def int_div(self, a: str, b: str) -> str:
        return f"FLOOR(({a}) / ({b}))"

    def mod(self, a: str, b: str) -> str:
        return f"MOD({a}, {b})"

    def decimal_text(self, expr: str, scale: int) -> str:
        return f"CAST(CAST({expr} AS NUMBER(38, {scale})) AS VARCHAR)"

    def date_text(self, expr: str) -> str:
        # The cast is a no-op for a DATE column; see format_timestamp.
        return f"TO_CHAR(CAST({expr} AS DATE), {self.date_format})"

    def format_timestamp(self, expr: str) -> str:
        # The explicit cast makes the argument's type visible to the formatter whatever
        # the column type. It is a no-op for TIMESTAMP_NTZ, drops the (UTC) offset from
        # the TIMESTAMP_TZ that aware_to_utc returns, and turns a DATE into midnight.
        # fakesnow drops the format of TO_CHAR on a bare DATE or TIMESTAMP_NTZ column
        # and returns DuckDB's default text (no trailing fractional zeros) without it.
        return f"TO_CHAR(CAST({expr} AS TIMESTAMP_NTZ), {self.timestamp_format})"

    def aware_to_utc(self, expr: str) -> str:
        return f"CONVERT_TIMEZONE('UTC', {expr})"

    def naive_to_utc(self, expr: str, zone: str) -> str:
        return f"CONVERT_TIMEZONE({self.literal(zone)}, 'UTC', {expr})"

    def date_to_timestamp(self, expr: str) -> str:
        return expr  # format_timestamp casts to TIMESTAMP_NTZ

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


def connect(config: dict[str, Any], name: str = "snowflake") -> DbApiAdapter:
    try:
        import snowflake.connector
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise AdapterError("install reconkit[snowflake] to connect to Snowflake") from exc
    try:
        conn = snowflake.connector.connect(**config)
    except Exception as exc:
        raise AdapterError(f"[{name}] cannot connect to Snowflake: {exc}") from exc
    return DbApiAdapter(SnowflakeDialect(), conn, name=name)
