"""Dialect (SQL generation) and Adapter (connection) base classes.

A dialect knows how to render the handful of SQL fragments reconkit needs so that every
engine produces *byte-identical* canonical text for the same logical value. An adapter
pairs a dialect with a DB-API connection and counts what it sends and receives.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from reconkit.errors import AdapterError
from reconkit.types import Canon, ColumnInfo, ColumnType, Kind, TableRef, is_quoted, unquote

Row = tuple[Any, ...]

#: Number of trailing hex digits of the MD5 kept as the row hash (15 digits = 60 bits, which
#: fits a signed 64-bit integer on every engine).
HASH_HEX_DIGITS = 15
HASH_SUBSTR_START = 32 - HASH_HEX_DIGITS + 1

_INTEGER_NAMES = {
    "smallint",
    "integer",
    "int",
    "bigint",
    "tinyint",
    "hugeint",
    "int2",
    "int4",
    "int8",
    "int64",
    "utinyint",
    "usmallint",
    "uinteger",
    "ubigint",
    "byteint",
}
_FLOAT_NAMES = {"real", "double", "double precision", "float", "float4", "float8", "float64"}
_TEXT_NAMES = {
    "text",
    "varchar",
    "character varying",
    "character",
    "char",
    "bpchar",
    "string",
    "nvarchar",
    "nchar",
    "name",
    "citext",
}
_DECIMAL_NAMES = {"numeric", "decimal", "number", "bignumeric", "bigdecimal"}


class Dialect:
    """SQL fragments for one engine. Subclasses override only what differs."""

    name = "ansi"
    text_type = "VARCHAR"
    #: DB-API parameter placeholder used for audit inserts.
    placeholder = "?"
    timestamp_format = "'YYYY-MM-DD HH24:MI:SS.US'"
    date_format = "'YYYY-MM-DD'"

    # -- identifiers and literals -------------------------------------------------------

    def quote(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    def fold(self, ident: str) -> str:
        """Case-folding applied by the engine to unquoted identifiers."""
        return ident.lower()

    def bare(self, part: str) -> str:
        """The catalogue spelling of one part of a table reference."""
        return unquote(part) if is_quoted(part) else self.fold(part)

    def table(self, ref: TableRef) -> str:
        return ".".join(ref.parts)

    def literal(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    # -- scalar building blocks ---------------------------------------------------------

    def cast_text(self, expr: str) -> str:
        return f"CAST({expr} AS {self.text_type})"

    def to_bigint(self, expr: str) -> str:
        return f"CAST({expr} AS BIGINT)"

    def int_div(self, a: str, b: str) -> str:
        """Integer division of two non-negative integers."""
        return f"(({a}) / ({b}))"

    def mod(self, a: str, b: str) -> str:
        return f"(({a}) % ({b}))"

    def md5_int(self, expr: str) -> str:
        """The last 60 bits of MD5(expr) as a non-negative 64-bit integer."""
        raise NotImplementedError

    def sum_hash(self, expr: str) -> str:
        """SUM over 60-bit hashes without overflowing."""
        return f"SUM({expr})"

    def concat(self, parts: Sequence[str]) -> str:
        return " || ".join(parts)

    # -- canonical text forms -----------------------------------------------------------

    def bool_text(self, expr: str, true: str, false: str) -> str:
        return f"CASE WHEN {expr} THEN '{true}' WHEN NOT {expr} THEN '{false}' END"

    def decimal_text(self, expr: str, scale: int) -> str:
        return f"CAST(CAST({expr} AS DECIMAL(38, {scale})) AS {self.text_type})"

    def float_input(self, expr: str) -> str:
        """Hook applied to float columns before they are rounded to a decimal."""
        return expr

    def date_text(self, expr: str) -> str:
        return f"TO_CHAR({expr}, {self.date_format})"

    def format_timestamp(self, expr: str) -> str:
        """Render a naive timestamp as ``YYYY-MM-DD HH:MM:SS.ffffff``."""
        return f"TO_CHAR({expr}, {self.timestamp_format})"

    def date_to_timestamp(self, expr: str) -> str:
        return f"CAST({expr} AS TIMESTAMP)"

    def aware_to_utc(self, expr: str) -> str:
        """Convert an instant (timestamptz) to a naive UTC timestamp."""
        return f"({expr} AT TIME ZONE 'UTC')"

    def naive_to_utc(self, expr: str, zone: str) -> str:
        """Interpret a naive timestamp as local time in ``zone`` and convert to naive UTC."""
        return f"(({expr} AT TIME ZONE {self.literal(zone)}) AT TIME ZONE 'UTC')"

    def timestamp_text(self, expr: str, src: ColumnType, naive_tz: str | None) -> str:
        if src.kind is Kind.DATE:
            expr = self.date_to_timestamp(expr)
        elif src.tz_aware:
            expr = self.aware_to_utc(expr)
        elif naive_tz:
            expr = self.naive_to_utc(expr, naive_tz)
        return self.format_timestamp(expr)

    def normalize(
        self, expr: str, src: ColumnType, canon: Canon, naive_tz: str | None = None
    ) -> str:
        """SQL producing the canonical text of ``expr`` (or NULL) for the given ``canon``."""
        kind = canon.kind
        if kind is Kind.BOOLEAN:
            return self.bool_text(expr, "true", "false")
        if kind is Kind.INTEGER:
            if src.kind is Kind.BOOLEAN:
                return self.bool_text(expr, "1", "0")
            return self.cast_text(expr)
        if kind is Kind.DECIMAL:
            if src.kind is Kind.BOOLEAN:
                expr = f"CASE WHEN {expr} THEN 1 WHEN NOT {expr} THEN 0 END"
            elif src.kind is Kind.FLOAT:
                expr = self.float_input(expr)
            return self.decimal_text(expr, canon.scale)
        if kind is Kind.DATE:
            return self.date_text(expr)
        if kind is Kind.TIMESTAMP:
            full = self.timestamp_text(expr, src, naive_tz if canon.shift_naive else None)
            if canon.ts_precision >= 6:
                return full
            length = 19 if canon.ts_precision <= 0 else 20 + canon.ts_precision
            return f"SUBSTR({full}, 1, {length})"
        if kind is Kind.UUID:
            return f"LOWER({self.cast_text(expr)})"
        out = self.cast_text(expr)
        if canon.trim:
            out = f"RTRIM({out})"
        if canon.empty_as_null:
            out = f"NULLIF({out}, '')"
        return out

    # -- catalogue ----------------------------------------------------------------------

    def columns_sql(self, ref: TableRef) -> str:
        """Query returning (name, data_type, numeric_precision, numeric_scale, is_nullable)."""
        where = [f"table_name = {self.literal(self.bare(ref.name))}"]
        if ref.schema:
            where.append(f"table_schema = {self.literal(self.bare(ref.schema))}")
        else:
            where.append(f"table_schema = {self.default_schema_sql()}")
        if ref.catalog:
            where.append(f"table_catalog = {self.literal(self.bare(ref.catalog))}")
        return (
            "SELECT column_name, data_type, numeric_precision, numeric_scale, is_nullable "
            f"FROM information_schema.columns WHERE {' AND '.join(where)} "
            "ORDER BY ordinal_position"
        )

    def default_schema_sql(self) -> str:
        return "current_schema()"

    def parse_type(self, native: str, precision: int | None, scale: int | None) -> ColumnType:
        """Map a catalogue type to a :class:`ColumnType`."""
        text = native.strip().lower()
        base = re.sub(r"\s*\(.*\)", "", text).strip()
        args = [int(a) for a in re.findall(r"\d+", text[len(base) :])]
        if base in _INTEGER_NAMES:
            return ColumnType(Kind.INTEGER, native=native)
        if base in _DECIMAL_NAMES:
            if scale is None and len(args) == 2:
                scale = args[1]
            elif scale is None and len(args) == 1:
                scale = 0
            return ColumnType(Kind.DECIMAL, scale=scale, native=native)
        if base in _FLOAT_NAMES:
            return ColumnType(Kind.FLOAT, native=native)
        if base in ("boolean", "bool"):
            return ColumnType(Kind.BOOLEAN, native=native)
        if base in _TEXT_NAMES:
            return ColumnType(Kind.TEXT, native=native)
        if base == "uuid":
            return ColumnType(Kind.UUID, native=native)
        if base == "date":
            return ColumnType(Kind.DATE, native=native)
        if base.startswith("timestamp") or base == "datetime":
            aware = "with time zone" in base or base in (
                "timestamptz",
                "timestamp_tz",
                "timestamp_ltz",
            )
            return ColumnType(Kind.TIMESTAMP, tz_aware=aware, native=native)
        return ColumnType(Kind.OTHER, native=native)


@dataclass
class QueryStats:
    """What an adapter sent and received. Used for reports and the benchmark."""

    queries: int = 0
    rows: int = 0
    seconds: float = 0.0

    def snapshot(self) -> QueryStats:
        return QueryStats(self.queries, self.rows, self.seconds)

    def since(self, earlier: QueryStats) -> QueryStats:
        return QueryStats(
            self.queries - earlier.queries,
            self.rows - earlier.rows,
            self.seconds - earlier.seconds,
        )


@runtime_checkable
class Adapter(Protocol):
    """The surface the diff engine and contract checker depend on."""

    name: str
    dialect: Dialect
    stats: QueryStats

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]: ...

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None: ...

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None: ...

    def get_columns(self, ref: TableRef) -> list[ColumnInfo]: ...

    def close(self) -> None: ...


class DbApiAdapter:
    """Adapter over any PEP 249 connection.

    Queries are serialised with a lock: most DB-API connections are not safe to use from
    two threads at once, and the diff engine queries the two sides concurrently.
    """

    def __init__(
        self, dialect: Dialect, connection: Any, *, name: str = "", autocommit: bool = True
    ) -> None:
        self.dialect = dialect
        self.name = name or dialect.name
        self.stats = QueryStats()
        self._connection = connection
        self._autocommit = autocommit
        self._lock = threading.Lock()
        #: Set to a list to record every SQL statement (used by tests and ``--explain``).
        self.sql_log: list[str] | None = None

    def _cursor(self) -> Any:
        return self._connection.cursor()

    def _run(self, sql: str, params: Sequence[Any], fetch: bool) -> list[Row]:
        if self.sql_log is not None:
            self.sql_log.append(sql)
        started = time.perf_counter()
        with self._lock:
            cursor = self._cursor()
            try:
                if params:
                    cursor.execute(sql, tuple(params))
                else:
                    cursor.execute(sql)
                rows = [tuple(r) for r in cursor.fetchall()] if fetch else []
                if not fetch and not self._autocommit:
                    self._connection.commit()
            except Exception as exc:
                raise AdapterError(f"[{self.name}] query failed: {exc}\n{sql[:2000]}") from exc
            finally:
                cursor.close()
        self.stats.queries += 1
        self.stats.rows += len(rows)
        self.stats.seconds += time.perf_counter() - started
        return rows

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return self._run(sql, params, fetch=True)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._run(sql, params, fetch=False)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self._lock:
            cursor = self._cursor()
            try:
                cursor.executemany(sql, [tuple(r) for r in rows])
                if not self._autocommit:
                    self._connection.commit()
            except Exception as exc:
                raise AdapterError(f"[{self.name}] insert failed: {exc}\n{sql[:2000]}") from exc
            finally:
                cursor.close()
        self.stats.queries += 1

    def get_columns(self, ref: TableRef) -> list[ColumnInfo]:
        rows = self.query(self.dialect.columns_sql(ref))
        if not rows:
            raise AdapterError(f"[{self.name}] table {ref} not found or has no columns")
        return [
            ColumnInfo(
                name=str(name),
                type=self.dialect.parse_type(
                    str(native),
                    int(precision) if precision is not None else None,
                    int(scale) if scale is not None else None,
                ),
                nullable=str(nullable).upper() != "NO",
            )
            for name, native, precision, scale, nullable in rows
        ]

    def close(self) -> None:
        self._connection.close()
