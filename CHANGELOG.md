# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-19

### Added

- `reconkit data-diff`, a drop-in for the archived data-diff command line: its four
  positional arguments and connection URIs (PostgreSQL, Redshift, Snowflake, BigQuery,
  DuckDB), `--key-columns`, `--update-column`, `--columns` with `%` wildcards,
  `--where`, `--min-age`/`--max-age`, `--limit`, `--algorithm`, the bisection settings,
  `--stats` and `--json` output in data-diff's formats, and `--conf`/`--run` TOML files.
  `--fail-on-diff` exits 1 when anything differs.
- `reconkit data-diff --dbt`: compares each model's dev build with production using
  data-diff's `vars: data_diff:` settings, primary-key detection (meta, tags, uniqueness
  tests) and per-model `meta: datafold: datadiff:` options, reading `dbt_project.yml`,
  `profiles.yml` and the manifest directly. `--select` supports a subset of dbt's
  selection syntax; `--state` takes production locations from a production manifest.
  Checked against the expected output in data-diff's own dbt test.
- docs/migrating-from-data-diff.md and examples/data-diff (one million rows, Postgres
  against DuckDB). Unlike data-diff, errors exit 2 (configuration) or 3 (query).
- Integration tests for the Snowflake, BigQuery and Redshift adapters against local
  emulators (fakesnow 0.12.1, goccy/bigquery-emulator 0.8.1, Floci 2.5.0), under a
  `docker compose --profile emulators` profile and a new CI job.
- BigQuery connections accept `api_endpoint`, `anonymous: true` and `keyfile`.

### Fixed

- Snowflake: backslashes in string literals were read as escape sequences.
- BigQuery: string literals containing a quote, backslash or newline were invalid
  GoogleSQL; the `INFORMATION_SCHEMA.COLUMNS` lookup used a spelling the emulator does
  not resolve, now one backtick-quoted path.
- Snowflake: timestamp, date and checksum-sum expressions carry explicit casts (no-ops on
  Snowflake) so that they give the same results where the column type is not visible to
  the function.

## [0.1.0] - 2026-11-17

First release.

### Added

- Cross-database table diff by checksum bisection, with range bucketing for integer keys
  and hash bucketing for uuid, text and composite keys.
- Same-connection fast path using a single `FULL OUTER JOIN`.
- Canonical normalisation of numerics, floats, booleans, dates, timestamps (including
  timezone-naive columns stored in a local zone), text and uuids across engines.
- Per-column absolute/relative numeric tolerances and timestamp tolerances; column
  mapping, ignore lists, row filters, deterministic key-hash sampling, diff limits.
- Adapters: PostgreSQL and DuckDB (integration-tested); Snowflake, BigQuery and Redshift
  SQL dialects (snapshot-tested only, not run against live accounts).
- Data contracts in the shape of the Open Data Contract Standard v3: `reconkit contract
  check` against a live table and `reconkit contract diff` to classify changes as
  breaking or non-breaking.
- Reports: terminal, JSON, JUnit XML, self-contained HTML. Append-only audit tables.
- `reconkit.yml` configuration with environment interpolation and rejection of
  plain-text secrets; `reconkit run`, `reconkit diff`, `reconkit validate`.
- Airflow operator and Dagster asset-check helper, importable without either installed.
- Reproducible benchmark script and a loan-origination example.
