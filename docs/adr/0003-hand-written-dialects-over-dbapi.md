# 0003. Hand-written SQL dialects over DB-API; sqlglot only as a test-time parser

Status: accepted

## Context

reconkit generates a small, fixed family of queries (about ten shapes) for several
engines. The options were SQLAlchemy Core, generating one dialect and transpiling with
sqlglot, or writing the fragments per engine by hand.

The property the tool depends on is unusual: not that a query *runs* on each engine, but
that specific expressions produce **byte-identical text** on each engine. Integer
division (`/` in Postgres is integer division, in DuckDB it is not), timestamp formatting
(`TO_CHAR` patterns against `strftime` against BigQuery's `%E6S`), float-to-decimal
rounding, the text form of a scaled decimal. A transpiler's contract is equivalent
syntax and best-effort semantics; it does not promise identical output strings, and
when it silently picks a slightly different function the result is a checksum mismatch
on every row.

## Decision

- A `Dialect` base class with roughly a dozen small methods, each returning a SQL
  fragment and each with a written contract (docs/adapters.md). One subclass per engine
  overrides only what differs; the PostgreSQL and DuckDB dialects are four methods each.
- Connections are plain PEP 249 objects wrapped by one `DbApiAdapter`. No SQLAlchemy:
  it would add a heavy dependency and its own type coercion between the driver and the
  canonical text, for no query-building benefit at this size.
- sqlglot is a **development** dependency only. Tests parse every generated statement
  with the target engine's grammar, which catches syntax errors in the dialects that
  cannot be run locally.
- Generated SQL is pinned by golden snapshots, so any change to it is visible in review.

## Consequences

- Runtime dependencies stay small: click, pydantic, pyyaml, rich, plus one driver per
  engine as an optional extra.
- Adding an engine means reading that engine's documentation for a dozen functions and
  writing them down. That is the actual work and no library removes it.
- Identifiers from the catalogue are always quoted. Table references from configuration
  are validated against a strict identifier grammar and emitted verbatim. `where`
  clauses and contract `sql` rules are passed through as written: they are code supplied
  by whoever controls the configuration, and are treated with the same trust as the
  configuration file itself. reconkit should not be exposed to untrusted configuration.
- The dialects for Snowflake, BigQuery and Redshift are written from documentation and
  have not been executed against those services. The snapshot and parse tests do not
  establish output equivalence. This is stated wherever those engines are mentioned.
