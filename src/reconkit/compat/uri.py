"""data-diff connection strings and ``[database.*]`` dictionaries, as reconkit connections.

data-diff identified a database by a URI such as ``postgresql://user:pass@host/db`` or, in
a ``--conf`` file, by a dictionary with a ``driver`` key. Both are translated here into a
reconkit connection: an adapter type plus the keyword arguments that adapter's ``connect``
takes. Drivers reconkit has no adapter for are refused with a clear message rather than
guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from reconkit.errors import ConfigError

#: data-diff drivers that reconkit cannot connect to (yet).
UNSUPPORTED_DRIVERS = frozenset(
    {"mysql", "oracle", "presto", "trino", "databricks", "clickhouse", "vertica", "mssql"}
)
SUPPORTED_DRIVERS = ("bigquery", "duckdb", "postgresql", "redshift", "snowflake")


@dataclass(frozen=True)
class Connection:
    """A reconkit adapter type and its ``connect`` arguments."""

    type: str
    config: dict[str, Any] = field(default_factory=dict)
    #: Prefix for table names given without one. BigQuery needs ``dataset.table``, and
    #: data-diff took the dataset from the URI path.
    default_namespace: str | None = None

    def qualify(self, table: str) -> str:
        if self.default_namespace and "." not in table:
            return f"{self.default_namespace}.{table}"
        return table

    def identity(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        """Two connections with the same identity can share one database session."""
        return self.type, tuple(sorted((k, repr(v)) for k, v in self.config.items()))


def _refuse(driver: str) -> ConfigError:
    if driver in UNSUPPORTED_DRIVERS:
        return ConfigError(
            f"data-diff driver {driver!r} has no reconkit adapter; supported: "
            f"{', '.join(SUPPORTED_DRIVERS)} (see docs/adapters.md to add one)"
        )
    return ConfigError(
        f"unknown database driver {driver!r}; supported: {', '.join(SUPPORTED_DRIVERS)}"
    )


def _path_parts(path: str) -> list[str]:
    return [unquote(p) for p in path.split("/") if p]


def from_uri(uri: str) -> Connection:
    """Translate a data-diff connection URI.

    Formats, as documented by data-diff::

        postgresql://<user>:<password>@<host>:<port>/<database>
        redshift://<user>:<password>@<host>:<port>/<database>
        snowflake://<user>:<password>@<account>/<database>/<SCHEMA>?warehouse=<WH>&role=<R>
        bigquery://<project>/<dataset>
        duckdb://<dbname>@<filepath>        (or duckdb://<filepath>)
    """
    scheme, sep, rest = uri.partition("://")
    if not sep:
        raise ConfigError(f"not a connection URI (expected driver://...): {uri!r}")
    driver = scheme.lower()
    if driver == "postgres":
        driver = "postgresql"

    if driver == "duckdb":
        # duckdb://<dbname>@<filepath>; the database name is DuckDB's catalog name and is
        # implied by the file, so only the path matters.
        _, at, file_path = rest.rpartition("@")
        file_path = file_path if at else rest
        return Connection("duckdb", {"path": unquote(file_path) or ":memory:"})

    if driver not in SUPPORTED_DRIVERS:
        raise _refuse(driver)

    parts = urlsplit(uri)
    query = dict(parse_qsl(parts.query))
    user = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password else None
    path = _path_parts(parts.path)

    if driver == "postgresql":
        # psycopg understands the URI itself, query parameters included (sslmode, ...).
        return Connection("postgres", {"url": "postgresql://" + rest})

    if driver == "redshift":
        cfg: dict[str, Any] = {"host": parts.hostname or "localhost", "port": parts.port or 5439}
        if user:
            cfg["user"] = user
        if password:
            cfg["password"] = password
        if path:
            cfg["database"] = path[0]
        cfg.update(query)
        return Connection("redshift", cfg)

    if driver == "snowflake":
        if not parts.hostname:
            raise ConfigError("snowflake URI needs an account: snowflake://user:pass@<account>/...")
        cfg = {"account": parts.netloc.rpartition("@")[2]}
        if user:
            cfg["user"] = user
        if password:
            cfg["password"] = password
        if len(path) >= 1:
            cfg["database"] = path[0]
        if len(path) >= 2:
            cfg["schema"] = path[1]
        for key, value in query.items():
            # data-diff called the private key file "key".
            cfg["private_key_file" if key == "key" else key] = value
        return Connection("snowflake", cfg)

    # bigquery://<project>/<dataset>
    if not parts.hostname:
        raise ConfigError("bigquery URI needs a project: bigquery://<project>/<dataset>")
    cfg = {"project": parts.netloc}
    cfg.update(query)
    return Connection("bigquery", cfg, default_namespace=path[0] if path else None)


#: data-diff dictionary keys that are spelled differently in reconkit's adapters.
_RENAMES: dict[str, dict[str, str]] = {
    "redshift": {"dbname": "database"},
    "snowflake": {"key": "private_key_file"},
    "duckdb": {"filepath": "path", "dbpath": "path"},
}


def from_dict(spec: dict[str, Any], name: str = "") -> Connection:
    """Translate a ``[database.<name>]`` table from a data-diff ``--conf`` file."""
    cfg = dict(spec)
    driver = str(cfg.pop("driver", "")).lower()
    if not driver:
        raise ConfigError(f"database {name!r} did not specify a driver")
    if driver == "postgres":
        driver = "postgresql"
    if driver not in SUPPORTED_DRIVERS:
        raise _refuse(driver)
    adapter = "postgres" if driver == "postgresql" else driver
    renames = _RENAMES.get(adapter, {})
    out = {renames.get(k, k): v for k, v in cfg.items()}
    if adapter == "duckdb":
        out.pop("database", None)
    namespace = None
    if adapter == "bigquery":
        namespace = out.pop("dataset", None)
    return Connection(adapter, out, default_namespace=namespace)
