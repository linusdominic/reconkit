"""BigQuery dialect and adapter.

Status: the SQL generation is unit-tested against golden snapshots and parsed with
sqlglot's BigQuery grammar. The adapter is integration-tested against
goccy/bigquery-emulator 0.8.1 (tests/integration/test_bigquery.py) through the real
google-cloud-bigquery client. The emulator parses GoogleSQL with Google's ZetaSQL front
end but evaluates it over SQLite, and its INT64 ``MOD`` is inexact above 2**53, so the
test substitutes ``mod``. It has NOT been run against a live BigQuery project.
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

    def literal(self, value: str) -> str:
        # GoogleSQL has no '' escape (it reads 'it''s' as two adjacent literals, a syntax
        # error), treats backslash as an escape character and rejects raw newlines.
        escaped = (
            value.replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f"'{escaped}'"

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
        # One backtick-quoted path, as for tables. The emulator does not resolve the
        # `dataset`.INFORMATION_SCHEMA.COLUMNS spelling used before.
        dataset = ".".join(unquote(p) for p in ref.parts[:-1])
        return (
            "SELECT column_name, data_type, NULL, NULL, is_nullable "
            f"FROM `{dataset}.INFORMATION_SCHEMA.COLUMNS` "
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


def client_options(config: dict[str, Any], name: str = "bigquery") -> dict[str, Any]:
    """Keyword arguments for ``google.cloud.bigquery.Client`` from a connection config.

    Besides the client's own arguments (``project``, ``location``, ...) this accepts:

    * ``api_endpoint``: base URL of the API, e.g. a local emulator.
    * ``anonymous: true``: no credentials at all (emulators only); needs ``project``.
    * ``keyfile``: path to a service-account JSON key file.

    Without ``anonymous`` or ``keyfile`` the client finds Application Default
    Credentials itself.
    """
    cfg = dict(config)
    endpoint = cfg.pop("api_endpoint", None)
    anonymous = bool(cfg.pop("anonymous", False))
    keyfile = cfg.pop("keyfile", None)
    if anonymous and keyfile:
        raise ConfigError(f"[{name}] set either 'anonymous' or 'keyfile', not both")
    if endpoint:
        options = dict(cfg.pop("client_options", None) or {})
        options["api_endpoint"] = str(endpoint)
        cfg["client_options"] = options
    if anonymous:
        if not cfg.get("project"):
            raise ConfigError(f"[{name}] 'anonymous: true' needs an explicit 'project'")
        from google.auth.credentials import AnonymousCredentials

        cfg["credentials"] = AnonymousCredentials()  # type: ignore[no-untyped-call]
    elif keyfile:
        from google.oauth2 import service_account

        try:
            load = service_account.Credentials.from_service_account_file
            cfg["credentials"] = load(str(keyfile))  # type: ignore[no-untyped-call]
        except (OSError, ValueError) as exc:
            raise ConfigError(f"[{name}] cannot read keyfile {keyfile!r}: {exc}") from exc
    return cfg


def connect(config: dict[str, Any], name: str = "bigquery") -> DbApiAdapter:
    try:
        from google.cloud import bigquery
        from google.cloud.bigquery import dbapi
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise AdapterError("install reconkit[bigquery] to connect to BigQuery") from exc
    kwargs = client_options(config, name)
    try:
        conn = dbapi.connect(bigquery.Client(**kwargs))  # type: ignore[no-untyped-call]
    except Exception as exc:
        raise AdapterError(f"[{name}] cannot connect to BigQuery: {exc}") from exc
    return DbApiAdapter(BigQueryDialect(), conn, name=name)
