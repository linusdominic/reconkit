# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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
