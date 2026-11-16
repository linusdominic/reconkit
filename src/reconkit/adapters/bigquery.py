"""BigQuery dialect and adapter.

Status: the SQL generation is unit-tested against golden snapshots and parsed with
sqlglot's BigQuery grammar. It has NOT been run against a live BigQuery project.
"""

from __future__ import annotations

from typing import Any

from reconkit.adapters.base import HASH_SUBSTR_START, DbApiAdapter, Dialect
from reconkit.errors import AdapterError, ConfigError
from reconkit.types import ColumnType, Kind, TableRef, unquote

_TS_FORMAT = "'%Y-%m-%d %H:%M:%E6S'"


class BigQueryDialect(Dialect):
    name = "bigquery"
    text_type = "STRING"
    placeholder = "%s"

    def quote(self, ident: str) -> str:
        return "`" + ident.replace("`", "") + "`"

    def fold(self, ident: str) -> str:
        return ident

    def table(self, ref: TableRef) -> str:
        return "`" + ".".join(unquote(p) for p in ref.parts) + "`"

    def to_bigint(self, expr: str) -> str:
        return f"CAST({expr} AS INT64)"

    def md5_int(self, expr: str) -> str:
        return f"CAST(CONCAT('0x', SUBSTR(TO_HEX(MD5({expr})), {HASH_SUBSTR_START})) AS INT64)"

    def sum_hash(self, expr: str) -> str:
        return f"SUM(CAST({expr} AS NUMERIC))"

    def int_div(self, a: str, b: str) -> str:
        return f"DIV({a}, {b})"

    def mod(self, a: str, b: str) -> str:
        return f"MOD({a}, {b})"

    def decimal_text(self, expr: str, scale: int) -> str:
        return f"FORMAT('%.{scale}f', ROUND(CAST({expr} AS BIGNUMERIC), {scale}))"

    def date_text(self, expr: str) -> str:
        return f"FORMAT_DATE('%Y-%m-%d', {expr})"

    def timestamp_text(self, expr: str, src: ColumnType, naive_tz: str | None) -> str:
        # BigQuery separates TIMESTAMP (an instant) from DATETIME (naive wall-clock time).
        if src.kind is Kind.DATE:
            return f"FORMAT_DATETIME({_TS_FORMAT}, DATETIME({expr}))"
        if src.tz_aware:
            return f"FORMAT_TIMESTAMP({_TS_FORMAT}, {expr}, 'UTC')"
        if naive_tz:
            zone = self.literal(naive_tz)
            return f"FORMAT_TIMESTAMP({_TS_FORMAT}, TIMESTAMP({expr}, {zone}), 'UTC')"
        return f"FORMAT_DATETIME({_TS_FORMAT}, {expr})"

    def columns_sql(self, ref: TableRef) -> str:
        if not ref.schema:
            raise ConfigError(f"BigQuery tables must be qualified as dataset.table: {ref}")
        dataset = ".".join(unquote(p) for p in ref.parts[:-1])
        return (
            "SELECT column_name, data_type, NULL, NULL, is_nullable "
            f"FROM `{dataset}`.INFORMATION_SCHEMA.COLUMNS "
            f"WHERE table_name = {self.literal(unquote(ref.name))} ORDER BY ordinal_position"
        )

    def parse_type(self, native: str, precision: int | None, scale: int | None) -> ColumnType:
        base = native.strip().upper()
        if base == "TIMESTAMP":
            return ColumnType(Kind.TIMESTAMP, tz_aware=True, native=native)
        if base == "NUMERIC":
            return ColumnType(Kind.DECIMAL, scale=9, native=native)
        if base == "BIGNUMERIC":
            return ColumnType(Kind.DECIMAL, scale=38, native=native)
        return super().parse_type(native, precision, scale)


def connect(config: dict[str, Any], name: str = "bigquery") -> DbApiAdapter:  # pragma: no cover
    try:
        from google.cloud import bigquery
        from google.cloud.bigquery import dbapi
    except ImportError as exc:
        raise AdapterError("install reconkit[bigquery] to connect to BigQuery") from exc
    try:
        conn = dbapi.connect(bigquery.Client(**config))
    except Exception as exc:
        raise AdapterError(f"[{name}] cannot connect to BigQuery: {exc}") from exc
    return DbApiAdapter(BigQueryDialect(), conn, name=name)
