# Adapters

## Status

| Engine | Dialect (SQL generation) | Run against a live database |
| --- | --- | --- |
| PostgreSQL | implemented | yes: integration tests in `tests/integration` (PostgreSQL 16) |
| DuckDB | implemented | yes: unit, property and integration tests |
| Snowflake | implemented | **no**: dialect implemented, not integration-tested against a live account |
| BigQuery | implemented | **no**: dialect implemented, not integration-tested against a live account |
| Redshift | implemented | **no**: dialect implemented, not integration-tested against a live account |

For the three unverified engines, the generated SQL is pinned by golden snapshots in
`tests/snapshots/` and each statement is parsed with sqlglot's grammar for that engine.
That catches syntax errors and accidental changes. It does **not** prove that, say,
Snowflake renders `NUMBER(38,2)` as text exactly the way Postgres renders
`numeric(38,2)`, which is the property the checksum depends on. Before trusting one of
these in production, run the type-matrix test described at the end of this page.
If the canonical forms disagree for some type, the failure mode is false differences
(rows reported as changed, with both values shown), not missed ones.

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
