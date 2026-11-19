# Adapters

## Status

| Engine | Dialect (SQL generation) | Run against a live database | Run against a local emulator |
| --- | --- | --- | --- |
| PostgreSQL | implemented | yes: integration tests in `tests/integration` (PostgreSQL 16) | not needed |
| DuckDB | implemented | yes: unit, property and integration tests | not needed |
| Snowflake | implemented | **no** | yes: fakesnow 0.12.1 |
| BigQuery | implemented | **no** | yes: goccy/bigquery-emulator 0.8.1 |
| Redshift | implemented | **no** | yes: Floci 2.5.0 (PostgreSQL 16 data plane) |

For Snowflake, BigQuery and Redshift the generated SQL is pinned by golden snapshots in
`tests/snapshots/`, each statement is parsed with sqlglot's grammar for that engine, and
the adapter runs end to end against a free local emulator (next section). None of the
three has been run against the real service. An emulator is not the service: it shows
that the adapter connects, that its catalogue query works and that its SQL is accepted
and produces matching checksums *in the emulator*. It does **not** prove that, say,
Snowflake renders `NUMBER(38,2)` as text exactly the way Postgres renders
`numeric(38,2)`, which is the property the checksum depends on, because fakesnow
computes that text with DuckDB. Before trusting one of these in production, run the
type-matrix test described at the end of this page against a real account.
If the canonical forms disagree for some type, the failure mode is false differences
(rows reported as changed, with both values shown), not missed ones.

## Emulator tests

Each engine has a module in `tests/integration/` that seeds the same 400-row table on
both sides, the engine under test on the right and DuckDB or the docker-compose Postgres
on the left, with planted differences: a row missing on each side, a decimal changed by
0.01, a value set to NULL and a NULL set to a value, a timestamp changed by one
microsecond, a naive timestamp changed by an hour, a trailing space, the same word in
NFC and NFD Unicode, a date, a boolean and a float changed. Identical rows on both sides
cover all-NULL rows, quotes, backslashes, newlines, non-Latin text, the empty string,
leading and trailing spaces, extreme decimals and timestamps written with a `+08:00`
offset. The tests require reconkit to report exactly the planted rows, with both range
and hash bucketing, through more than one bisection level, downloading only the
mismatching buckets; and with `trim_whitespace` the trailing-space change disappears.
Each module also checks the catalogue types and `naive_timezone`.

```bash
docker compose up -d --wait postgres
docker compose --profile emulators up -d bigquery floci   # Snowflake needs no container
RECONKIT_REQUIRE_PG=1 RECONKIT_REQUIRE_SNOWFLAKE=1 RECONKIT_REQUIRE_BIGQUERY=1 \
  RECONKIT_REQUIRE_REDSHIFT=1 uv run pytest tests/integration
docker compose --profile emulators down
```

Without the `RECONKIT_REQUIRE_<ENGINE>=1` variables a missing emulator skips its tests;
with them it fails them. CI runs the Snowflake tests in the test matrix (Python 3.11
and later) and the BigQuery and Redshift tests in the `emulators` job.

### Snowflake: fakesnow 0.12.1

[fakesnow](https://github.com/tekumara/fakesnow) patches `snowflake.connector` in
process, parses each statement as Snowflake SQL with sqlglot and runs it on DuckDB. It
needs Python 3.11+ and does not work with snowflake-connector-python 5.x
(tekumara/fakesnow#450), so the dev dependencies pin the connector below 5 and the
tests ran with connector 4.8.0. Chosen because it needs no container, account or
network, so it also runs in the ordinary CI test matrix.

Exercised: `snowflake.connector.connect`, DB-API cursors, the `information_schema`
query, upper-case folding of unquoted names and database/schema qualifiers, and every
dialect fragment except `md5_int`.

Not verified, because fakesnow does not run it or computes it with DuckDB:

- `md5_int`: fakesnow raises `NotImplementedError` for `TO_NUMBER(<text>, 'XXX...')`,
  so the test swaps in an equivalent DuckDB-style cast. The production expression has
  only been parsed, never run.
- Every value semantic: number-to-text casts, rounding, `TO_CHAR` formats,
  `CONVERT_TIMEZONE`, `TIMESTAMP_TZ` offsets and float behaviour are DuckDB's, as
  translated by sqlglot. In fakesnow, for example, `FLOOR(a / b)` returns a DOUBLE.
- Session parameters (`TIMEZONE`, `TIMESTAMP_TYPE_MAPPING`), warehouses, roles,
  authentication and connector 5.x.

### BigQuery: goccy/bigquery-emulator 0.8.1

[bigquery-emulator](https://github.com/goccy/bigquery-emulator) serves the BigQuery REST
API, parses GoogleSQL with go-zetasql (Google's ZetaSQL front end) and evaluates it over
SQLite. The tests use the real `google-cloud-bigquery` client with
`api_endpoint: http://127.0.0.1:59050` and `anonymous: true`. Chosen because it implements the
REST API the official client speaks, with Google's GoogleSQL parser, in one public
image (linux/amd64 and arm64).

Exercised: the client and its DB-API layer, `INFORMATION_SCHEMA.COLUMNS`, string
literal escaping, `TO_HEX(MD5())` to INT64, `DIV`, `FORMAT` on `BIGNUMERIC`,
`FORMAT_TIMESTAMP` / `FORMAT_DATETIME` / `FORMAT_DATE`, `TIMESTAMP(datetime, zone)`, and
`SUM(NUMERIC)` of 60-bit hashes.

Not verified:

- `mod`: the emulator computes INT64 `MOD` through floating point, so it is wrong
  above 2**53 (`MOD(9007199254740993, 2)` is 0). The test swaps in `a - b * DIV(a, b)`
  and has a test that fails once the emulator is fixed. Production `MOD` is not run.
- Catalogue details: the emulator reports `NUMERIC(14, 2)` as `NUMERIC` (BigQuery
  reports the parameters) and `FLOAT64` as `DOUBLE`, so the parameterised-type path of
  `parse_type` is only unit-tested.
- Function results come from the emulator's Go implementations, not BigQuery's.
- Authentication: only anonymous credentials ran. A `keyfile` is loaded and checked
  locally, but no token was ever requested. Locations, quotas, slots and cost.

The emulator answers some query errors with HTTP 500, which the client retries for
several minutes, so a failing query against it can look like a hang.

### Redshift: Floci 2.5.0

[Floci](https://github.com/floci-io/floci) (MIT) emulates the Redshift control plane
and runs each cluster as a PostgreSQL container behind an auth proxy and a SQL
interceptor. The test creates a cluster with boto3 (`CreateCluster`, dummy credentials,
`http://127.0.0.1:45670`), reads host and port from `DescribeClusters`, connects with
`redshift_connector` and deletes the cluster afterwards. The cluster database here is
`postgres:16-alpine` (`FLOCI_SERVICES_REDSHIFT_IMAGE_VERSION` in docker-compose.yml).
Floci's proxy has a self-signed certificate, so the test connects with `ssl: false`.
Chosen because it is MIT-licensed and implements the Redshift API (CreateCluster,
DescribeClusters, DeleteCluster) that a real deployment is driven through.

Exercised: the `redshift_connector` connection and autocommit path, the
`information_schema` query, Redshift table DDL (`DISTSTYLE`, `DISTKEY`, `SORTKEY`,
stripped by Floci), and the dialect's SQL with PostgreSQL semantics.

The Redshift dialect relies on these functions; which ones the emulator really ran:

| Function or construct | Redshift-specific? | Ran on Floci |
| --- | --- | --- |
| `STRTOL(text, 16)` | yes | **no**: PostgreSQL has none; the test installs a SQL stand-in |
| `MD5`, `SUBSTRING` | no | yes, PostgreSQL's |
| `TO_CHAR(..., 'YYYY-MM-DD HH24:MI:SS.US')`, `TO_CHAR(..., 'YYYY-MM-DD')` | format codes differ between engines | yes, PostgreSQL's |
| `AT TIME ZONE` on `TIMESTAMPTZ` and `TIMESTAMP` | semantics differ between engines | yes, PostgreSQL's |
| `CAST(... AS VARCHAR(65535))` | yes (bare `VARCHAR` is 256 bytes in Redshift) | yes, but PostgreSQL does not truncate |
| `CAST(... AS DOUBLE PRECISION)` before rounding floats | no | yes, but only on a `DOUBLE PRECISION` column (the `REAL` case did not run) |
| `SUM(CAST(... AS DECIMAL(38, 0)))` | overflow rules differ | yes, PostgreSQL's |
| `/` and `%` on `BIGINT`, `RTRIM`, `NULLIF`, `LOWER`, `current_schema()` | no | yes, PostgreSQL's |

So on Redshift itself none of `STRTOL`, the `TO_CHAR` output, time zone conversion,
`VARCHAR` length limits, numeric overflow, string-literal escaping or Redshift's
`information_schema` (which omits late-binding views and external tables) has been
verified.

### Connection options added for the emulators

BigQuery connections accept, besides the client's own `project` and `location`:
`api_endpoint` (passed as `client_options={"api_endpoint": ...}`), `anonymous: true`
(`AnonymousCredentials`, needs `project`) and `keyfile` (a service-account JSON file).
A project id with a hyphen must be backtick-quoted in a table reference:
`` `my-project`.dataset.table ``.

## Why hand-written dialects

See [ADR 0003](adr/0003-hand-written-dialects-over-dbapi.md). In short: the checksum
requires every engine to produce byte-identical text for the same value, and that is a
semantic property of specific functions (`to_char` versus `strftime`, integer versus
float division, how a float is rounded to a decimal). A transpiler guarantees equivalent
syntax, not identical output, so each fragment is written and tested per engine.
Connections are plain DB-API (PEP 249); there is no ORM or SQLAlchemy layer.

## Writing an adapter

An adapter is two things: a `Dialect` subclass that renders about a dozen SQL fragments,
and a function that opens a DB-API connection and wraps it in `DbApiAdapter`.

```python
from typing import Any

from reconkit.adapters import DbApiAdapter, Dialect, register
from reconkit.adapters.base import HASH_SUBSTR_START


class MySQLDialect(Dialect):
    name = "mysql"
    text_type = "CHAR"
    placeholder = "%s"

    def quote(self, ident: str) -> str:
        return "`" + ident.replace("`", "``") + "`"

    def md5_int(self, expr: str) -> str:
        # last 60 bits of the MD5 as a non-negative integer
        return f"CAST(CONV(SUBSTR(MD5({expr}), {HASH_SUBSTR_START}), 16, 10) AS UNSIGNED)"

    def int_div(self, a: str, b: str) -> str:
        return f"(({a}) DIV ({b}))"

    def concat(self, parts):  # '||' is logical OR in MySQL by default
        return f"CONCAT({', '.join(parts)})"

    def format_timestamp(self, expr: str) -> str:
        return f"DATE_FORMAT({expr}, '%Y-%m-%d %H:%i:%s.%f')"

    def date_text(self, expr: str) -> str:
        return f"DATE_FORMAT({expr}, '%Y-%m-%d')"

    # ... aware_to_utc, naive_to_utc, decimal_text, parse_type as needed


def connect(config: dict[str, Any], name: str) -> DbApiAdapter:
    import pymysql

    return DbApiAdapter(MySQLDialect(), pymysql.connect(**config), name=name)


register("mysql", connect)  # now `type: mysql` works in reconkit.yml
```

(This MySQL sketch is illustrative and untested. It is here to show the shape, and it
also shows a gap: reconkit's row-hash builder uses `dialect.concat` for the column list
but `||` for the `'V' || value` NULL encoding, so an engine without `||` needs
`PIPES_AS_CONCAT` or a small change in `SideSql.enc`.)

### The fragments, and what each must guarantee

| Method | Contract |
| --- | --- |
| `md5_int(expr)` | the last 15 hex digits of `MD5(expr)` as a non-negative 64-bit integer; `expr` is UTF-8 text |
| `sum_hash(expr)` | sum of those integers without overflow (cast to a wide decimal if `SUM(BIGINT)` is 64-bit) |
| `int_div(a, b)`, `mod(a, b)` | exact integer division and modulo of non-negative integers |
| `cast_text(expr)` | integers as plain digits, text unchanged and not truncated |
| `decimal_text(expr, scale)` | fixed `scale` decimals, rounded half away from zero, no thousands separators, leading zero before the point (`-0.50`) |
| `bool_text` | exactly the given true/false strings, NULL for NULL |
| `date_text` | `YYYY-MM-DD` |
| `format_timestamp` | `YYYY-MM-DD HH:MM:SS.ffffff`, 24-hour clock, six fractional digits |
| `aware_to_utc`, `naive_to_utc` | a naive timestamp in UTC, independent of the session time zone |
| `float_input` | hook to widen floats before decimal rounding (Postgres needs it for `real`) |
| `columns_sql`, `parse_type` | catalogue query and mapping of native type names to `ColumnType` |
| `quote`, `fold`, `table` | identifier quoting and the engine's case folding of unquoted names |

`reconkit.adapters.Adapter` is a `Protocol`; if your driver is not DB-API shaped you can
implement `query`, `execute`, `executemany`, `get_columns` and `close` directly instead of
using `DbApiAdapter`.

### Proving it

Copy `tests/integration/test_postgres.py::test_type_matrix_is_identical_across_engines`
and `test_postgres_vs_duckdb_matches_oracle`, replace the Postgres side with your engine,
and run them. The first loads the same rows of every supported type into your engine and
DuckDB and requires zero differences; the second is the property-based test against a
brute-force oracle. An adapter that passes both is "integration-tested" in the sense used
in the table above. Real bugs these tests found while building the Postgres adapter:
Postgres converts `real` to `numeric` through six significant digits (`1048576` became
`1048580`), which is now handled by `float_input`.
