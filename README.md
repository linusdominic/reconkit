# reconkit

Reconcile two tables that live in different databases without downloading them, and
check tables against data contracts.

```
$ reconkit run --all
FAIL  loans  3 missing in left, 7 missing in right, 16 changed  0.36s
  left   oltp:lending.loans  50,000 rows  4 queries, 2,880 rows downloaded
  right  warehouse:analytics.fct_loans  49,996 rows  4 queries, 2,876 rows downloaded
  strategy bisect/range, 1 level(s), 23 rows within tolerance
  ...
│ changed          │ 1405    │ originated_at    │ 2024-03-22 10:56:02.000000 │ 2024-03-22 02:56:02.000000 │
│ changed          │ 4399    │ principal_amount │ 419425.89                  │ 419425.90                  │
```

(Output of the bundled example, abridged: a 50,000-row Postgres table against its DuckDB
copy.)

Status: version 0.2.0, alpha. PostgreSQL and DuckDB are integration-tested. Snowflake,
BigQuery and Redshift are integration-tested against local emulators (fakesnow,
goccy/bigquery-emulator and Floci) but have **not** been run against live accounts. Not
published to PyPI; install from source. See [Limitations](#limitations) before relying
on it.

## The problem

If you have owned a pipeline that copies a system of record into a warehouse, you have
been asked whether the two agree, usually when a number in a report does not match a
number on a screen. For a loan book the question is concrete: is every loan in the
origination system in the warehouse, exactly once, with the same principal, rate and
status?

The honest ways to answer are all unpleasant:

- **Compare row counts and a few sums.** Cheap, and it tells you *that* something is
  wrong, not *which* rows. It also passes when two errors cancel.
- **Export both tables and diff them.** Correct, and it moves every row over the network
  every night to confirm that nothing changed. It stops being practical somewhere in the
  tens of millions of rows.
- **Write a bespoke SQL comparison per table.** Works when both tables are in one
  database. Across two engines you first have to make `numeric(14,2)` equal a `DOUBLE`,
  `timestamptz` equal a naive local-time `TIMESTAMP`, and `CHAR(6)` equal a `VARCHAR`,
  and every team rediscovers those rules separately.

Most of the work in reconciliation is not finding differences. It is not reporting false
ones: the eight-hour timezone shift on every row, the rate that differs in the seventh
decimal, the trailing spaces. A diff that reports 50,000 differences when the true
number is 16 gets switched off.

reconkit asks each database to checksum its own rows in buckets, downloads only the
buckets that disagree, and treats type normalisation and tolerances as the core of the
job, not an afterthought. It also checks tables against a written contract, because a
copy can match its source perfectly and still not be what downstream users were promised.

## What it does

- **Cross-database diff by checksum bisection.** Reports rows missing on either side and
  rows that changed, with the differing columns and both values. Composite keys, NULLs,
  integer, uuid and text keys.
- **Normalisation across engines.** Decimal scale, floats, booleans stored as integers,
  dates against timestamps, timezone-aware against naive timestamps (including naive
  columns stored in a local zone), trailing whitespace, empty string against NULL.
- **Tolerances and rules.** Absolute and relative numeric tolerance and timestamp
  tolerance per column; column renames; ignored columns; row filters; deterministic
  sampling.
- **Same-database fast path.** One `FULL OUTER JOIN` when both tables share a connection.
- **Data contracts** in the shape of the Open Data Contract Standard: check a live table
  (`reconkit contract check`), and classify changes between two contract versions as
  breaking or non-breaking for CI (`reconkit contract diff`).
- **Reports** for people and machines: terminal, JSON, JUnit XML, self-contained HTML.
  Stable exit codes. Optional append-only audit tables in a database of your choice.
- **Integrations:** an Airflow operator, a Dagster asset-check helper, a GitHub Actions
  workflow to copy.
- **A drop-in for data-diff.** `reconkit data-diff` takes the archived data-diff's
  arguments, connection URIs, TOML files and `--dbt` mode, and prints its output formats.
  See [Coming from data-diff](#coming-from-data-diff).

## Quickstart

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this repository> reconkit && cd reconkit
./examples/loans/run.sh
```

With the Postgres image already pulled this took under three seconds on the machine
described under [Benchmark](#benchmark); the first run also has to pull the image and
resolve dependencies. That one command starts a Postgres container on port 55439, seeds a 50,000-row
loan-origination table in it, seeds the "warehouse" copy in a local DuckDB file with the
kind of drift a real ETL produces (a renamed column, a `DOUBLE` where the source has
`numeric(7,4)`, naive Singapore-time timestamps, lost `CHAR` padding) plus a planted set
of real discrepancies, then reconciles the two and checks the warehouse table against its
contract. The seed script prints what it planted so you can compare it with what is
reported. HTML, JSON and JUnit reports are written to `examples/loans/`.

The configuration it runs is [examples/loans/reconkit.yml](examples/loans/reconkit.yml):

```yaml
connections:
  oltp:
    type: postgres
    host: ${RECONKIT_PG_HOST:-localhost}
    port: ${RECONKIT_PG_PORT:-55439}
    dbname: reconkit
    user: reconkit
    password: ${RECONKIT_PG_PASSWORD}   # secrets must be environment references
  warehouse:
    type: duckdb
    path: warehouse.duckdb

audit:                                  # optional: record every run and every diff row
  connection: warehouse
  table: analytics.recon_audit

checks:
  loans:
    left:  {connection: oltp, table: lending.loans}
    right:
      connection: warehouse
      table: analytics.fct_loans
      naive_timezone: Asia/Singapore    # naive timestamps on this side are local time
    key: loan_id
    mapping: {status: loan_status}      # left name: right name
    tolerances:
      interest_rate: {abs: 0.0005}
    options:
      trim_whitespace: true

contracts:
  fct_loans:
    connection: warehouse
    contract: contracts/fct_loans.yml
```

Remove the `naive_timezone` line and run it again to see what an unreconciled timezone
convention looks like: every row is reported.

When you are done: `docker compose down -v`.

## Coming from data-diff

[data-diff](https://github.com/datafold/data-diff) was archived in May 2024 and its main
fork, reladiff, was last updated in August 2025. If you have scripts, CI jobs or dbt
workflows that call it, replace `data-diff` with `reconkit data-diff`:

```bash
reconkit data-diff "postgresql://reconkit:${RECONKIT_PG_PASSWORD}@localhost:55439/reconkit" \
    servicing.payments duckdb://examples/data-diff/replica.duckdb payments \
    -k payment_id -c amount -c status
```

That is one of the commands in the bundled example, which seeds the two tables first:

```
$ ./examples/data-diff/run.sh
seeded 1,000,000 payments in Postgres servicing.payments and replica.duckdb
planted in the copy:
  deleted (missing in B):        [72918, 399914, 791004]
  added (missing in A):          [1000001, 1000002]
  amount changed by one cent:    [120392, 515730, 807122]
  status changed:                [278676, 348588, 464681]

== the rows that differ (key, amount, status) ==
- 72918, 813.13, SETTLED
- 120392, 3486.44, SETTLED
+ 120392, 3486.45, SETTLED
- 278676, 3874.47, SETTLED
+ 278676, 3874.47, RETURNED
...
+ 1000001, 100.00, SETTLED
+ 1000002, 100.00, SETTLED

real	0m3.820s

== --stats ==
1000000 rows in table A
999999 rows in table B
3 rows exclusive to table A (not present in B)
2 rows exclusive to table B (not present in A)
6 rows updated
999991 rows unchanged
0.00% difference score

Extra-Info:
  queries_A = 4
  queries_B = 4
  rows_downloaded = 1151
  strategy = bisect/range
  ...
```

That is a million-row Postgres table against its DuckDB copy on a laptop (both local).
All eleven planted differences are reported and nothing else, using four queries per
side and 1,151 downloaded rows. The 3.8 seconds are the first command's wall-clock time,
including starting `uv` and Python; in the `--stats` run the diff itself took 1.06 s. The same script then runs the comparison from a data-diff TOML
file (`--conf`, `--run`) and as a CI gate with `--fail-on-diff`.

What carries over unchanged: the four positional arguments and URI formats for Postgres,
Redshift, Snowflake, BigQuery and DuckDB; `-k`, `-t`, `-c` with `%` wildcards, `-w`,
`--min-age`, `--max-age`, `-l`, `-a`, the bisection settings; `--stats` and `--json`;
`--conf`/`--run`; and `--dbt` with data-diff's `vars: data_diff:` settings, primary-key
detection and per-model `meta`. What differs: errors exit non-zero (data-diff exited 0),
`--fail-on-diff` is new, and join-diff's materialisation options and Datafold Cloud are
refused. The full list is in
[docs/migrating-from-data-diff.md](docs/migrating-from-data-diff.md).

The dbt mode is checked against data-diff's own expectations: its test suite ships a dbt
project with dev and prod schemas and the output `data-diff --dbt` should print for it.
That project is in `tests/fixtures/`, and `reconkit data-diff --dbt` reproduces the
expected row counts, per-column counts and schema-change lines. reconkit has not been run
side by side with the data-diff executable itself.

## Using it

Install from a clone (the package is not on PyPI):

```bash
uv sync                                   # development environment, or:
pip install ".[postgres,duckdb]"          # extras: postgres duckdb snowflake bigquery redshift airflow dagster
```

Commands:

```bash
reconkit run --all                        # every check and contract in reconkit.yml
reconkit run loans --junit out.xml --html out.html --json out.json
reconkit diff --left oltp:lending.loans --right warehouse:analytics.fct_loans -k loan_id
reconkit contract check contracts/fct_loans.yml --connection warehouse
reconkit contract diff old.yml new.yml --format markdown --require-major-bump
reconkit validate --connect               # config is valid and connections open
```

Exit codes are stable, so CI and orchestrators can branch on them:

| Code | Meaning |
| --- | --- |
| 0 | every check passed; no breaking contract change |
| 1 | differences found, a contract rule failed, or a breaking contract change |
| 2 | invalid configuration, contract file or command line |
| 3 | a check could not run (connection failure, query error, missing table) |

From Python:

```python
from decimal import Decimal
from reconkit import DiffOptions, TableSpec, Tolerance, diff_tables
from reconkit.adapters import create_adapter

oltp = create_adapter("postgres", {"host": "db", "dbname": "lending", "user": "recon"})
wh = create_adapter("duckdb", {"path": "warehouse.duckdb"})

result = diff_tables(
    TableSpec(oltp, "lending.loans"),
    TableSpec(wh, "analytics.fct_loans", naive_timezone="Asia/Singapore"),
    key=["loan_id"],
    mapping={"status": "loan_status"},
    tolerances={"interest_rate": Tolerance(abs=Decimal("0.0005"))},
    options=DiffOptions(trim_whitespace=True),
)
for diff in result.diffs:
    print(diff.kind, diff.key, [(c.column, c.left, c.right) for c in diff.columns])
```

Check options (under `options:` per check, or `defaults:` for all):

| Option | Default | Meaning |
| --- | --- | --- |
| `strategy` | `auto` | `join` when both tables share a connection, else `bisect` |
| `bucketing` | `auto` | `range` for an integer first key column, else `hash` |
| `bisection_threshold` | 64 | buckets with at most this many rows are downloaded |
| `bisection_factor` | 32 | children per mismatching bucket |
| `initial_buckets` | auto | buckets in the first pass; sized from the row count |
| `float_precision` | 6 | decimal places floats are compared at |
| `timestamp_precision` | 6 | fractional-second digits compared (truncating) |
| `trim_whitespace` | false | ignore trailing whitespace in text |
| `empty_string_as_null` | false | treat `''` as NULL |
| `sample` | none | compare a deterministic key-hash sample, e.g. `0.01` |
| `limit` | none | stop after this many differing rows |
| `validate_unique_key` | false | fail first if the key is not unique on either side |

Per check you can also set `columns`, `ignore`, `mapping`, `tolerances`
(`abs`, `rel`, `seconds`) and `precision` (decimal places or fractional-second digits
for one column). Each side takes `where` (a SQL predicate in that database's dialect)
and `naive_timezone`.

### Orchestrators and CI

```python
# Airflow: fails the task on differences (exit code semantics as above)
from reconkit.integrations.airflow import ReconkitOperator

ReconkitOperator(
    task_id="reconcile_loans", config_path="/opt/airflow/reconkit.yml", checks=["loans"]
)

# Dagster: an asset check that passes only when the reconkit check passes
from reconkit.integrations.dagster import build_asset_check

loans_reconciled = build_asset_check(asset="fct_loans", config_path="reconkit.yml", check="loans")
```

Both modules import without Airflow or Dagster installed. A GitHub Actions workflow with
a nightly reconciliation job and a pull-request gate on contract changes is in
[examples/github-actions/reconcile.yml](examples/github-actions/reconcile.yml).

## How it works

```mermaid
flowchart TD
    A[Read both catalogues, pick one canonical text form per column pair] --> B{Same connection?}
    B -- yes --> J[One FULL OUTER JOIN returns only rows that differ]
    B -- no --> C[Each database returns bucket, row count, checksum for the whole table]
    C --> D{Bucket pairs equal?}
    D -- all equal --> M[Match. No table rows downloaded]
    D -- some differ --> E{Bucket small enough?}
    E -- yes --> F[Download those buckets from both sides]
    E -- no --> G[Checksum the children of the mismatching buckets only]
    G --> D
    F --> H[Compare rows locally, apply tolerances]
    J --> H
    H --> R[missing in left / missing in right / changed, per column]
```

1. **Normalise in SQL.** Each compared column is rendered, inside its own database, to a
   canonical text form chosen from both sides' types: `1234.50`, `2024-03-10
   01:30:00.000000` in UTC, `true`. NULL is encoded separately from every value.
2. **Hash and sum.** A row's hash is 60 bits of the MD5 of its canonical columns. A
   bucket's checksum is the row count plus the sum of row hashes. Both are things every
   engine computes identically and in parallel.
3. **Bucket and compare.** For an integer key, buckets are key ranges, so the database
   can use the primary-key index when asked about specific buckets. For uuid, text or
   composite keys, buckets are `hash(key) mod M`. Each level is one `GROUP BY` query per
   side, however many buckets mismatch.
4. **Decide locally.** Rows from mismatching buckets are downloaded as canonical text and
   compared. Tolerances are applied here, exactly, not approximated in the checksum.

The details, including the failure modes of each step, are in
[docs/algorithm.md](docs/algorithm.md).

## Benchmark

Measured with [benchmarks/bench.py](benchmarks/bench.py), which generates the same
table in Postgres and DuckDB (a `bigint` key and seven payload columns), injects a known
set of differences, and fails unless every strategy reports exactly those rows. The
baseline streams `SELECT * ... ORDER BY id` from both databases and merge-compares in
Python. Method and caveats are in [benchmarks/README.md](benchmarks/README.md); raw
output is in [benchmarks/results/](benchmarks/results/).

**1,000,000 rows, 1,000 differing rows (median of 5 runs, min to max in brackets)**

| Scenario | Strategy | Wall time | Queries | Rows transferred | of which table rows |
| --- | --- | ---: | ---: | ---: | ---: |
| identical | reconkit, range buckets | 1.15 s (0.77 to 1.50) | 6 | 31,268 | 0 |
| identical | reconkit, hash buckets | 1.16 s (1.03 to 1.25) | 6 | 62,518 | 0 |
| 1,000 differ | reconkit, range buckets | 1.48 s (1.16 to 1.85) | 14 | 155,146 | 123,878 |
| 1,000 differ | reconkit, hash buckets | 3.47 s (3.08 to 4.47) | 8 | 126,370 | 63,872 |
| 1,000 differ | download both and compare | 2.83 s (2.57 to 4.67) | 2 | 1,999,334 | 1,999,334 |

**10,000,000 rows, 1,000 differing rows (median of 3 runs)**

| Scenario | Strategy | Wall time | Queries | Rows transferred | of which table rows |
| --- | --- | ---: | ---: | ---: | ---: |
| identical | reconkit, range buckets | 30.18 s (27.17 to 50.99) | 6 | 125,018 | 0 |
| identical | reconkit, hash buckets | 24.41 s (24.06 to 31.18) | 6 | 524,306 | 0 |
| 1,000 differ | reconkit, range buckets | 26.89 s (23.42 to 30.77) | 22 | 197,584 | 9,334 |
| 1,000 differ | reconkit, hash buckets | 46.94 s (46.62 to 76.14) | 8 | 602,046 | 77,740 |
| 1,000 differ | download both and compare | 47.24 s (44.79 to 49.94) | 2 | 19,999,334 | 19,999,334 |

"Queries" and "rows transferred" are both sides added together; rows transferred
includes checksum rows (three integers each).

What these numbers do and do not show:

- **Rows transferred is the result that generalises.** With 1,000 differing rows,
  reconkit with range buckets moved 7.8% of the rows the download moved at one million
  rows and 1.0% at ten million. For identical tables it moved no table rows at all.
- **Wall time on localhost is a modest win, not a dramatic one.** Range bucketing was
  about 1.9 times faster than the download at one million rows and about 1.8 times at ten
  million. Hash bucketing was slower than the download at one million and level with it
  at ten million. Both databases were on the same machine, so the download paid almost
  nothing per row for transport, which is exactly the cost bisection exists to avoid.
  Nothing here measures a real network or a cloud warehouse.
- **The timings are noisy.** The machine was shared: other containers were running and
  the one-minute load average was between 8 and 17 on 10 cores throughout. The same
  configuration measured twice in one session (the default row above and the default row
  of the sweep below) gave medians of 1.48 s and 2.22 s. Treat differences under about
  1.5 times as within noise. Query and row counts are deterministic.
- **The ten-million-row runs are I/O-bound in Postgres.** The table does not fit in the
  container's 1 GB of memory, which is why ten times the rows took 17 to 26 times as
  long. reconkit's time at that size is almost entirely Postgres computing MD5 over every
  row in the first pass.
- **1,000 uniformly random differences is an unfavourable layout** for bisection: nearly
  every difference lands in its own bucket. Clustered differences touch fewer buckets.

**Why the defaults are what they are** (1,000,000 rows, 1,000 differing rows, range
buckets, median of 5 runs):

| `bisection_factor` | `bisection_threshold` | `initial_buckets` | Wall time | Queries | Rows transferred | Levels |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 32 | 16384 | 32 | 6.12 s | 16 | 1,279,224 | 2 |
| 32 | 1024 | 32 | 9.40 s | 14 | 1,269,210 | 2 |
| 32 | 64 | 32 | 11.64 s | 22 | 103,400 | 3 |
| 32 | 64 | 1024 | 3.65 s | 20 | 103,336 | 2 |
| 32 | 16 | auto | 2.62 s | 14 | 156,096 | 1 |
| **32** | **64** | **auto** | 2.22 s | 14 | 155,146 | 1 |
| 32 | 256 | auto | 3.03 s | 14 | 466,430 | 1 |
| 8 | 64 | auto | 2.74 s | 14 | 155,146 | 1 |

The first row uses a first pass of 32 buckets and a 16,384-row download threshold,
which to my knowledge were data-diff's defaults (applied here to reconkit's
implementation; data-diff itself was not run). With 1,000 scattered differences every one of the 32 buckets
mismatches, the table is checksummed twice, and most of it is downloaded anyway. A fine
first pass costs about thirty thousand checksum rows and removes the second scan, which is
why `initial_buckets` is sized from the row count by default.

Machine: Apple M5, 10 cores, macOS 26.6.2 (arm64); Python 3.12.14; PostgreSQL 16.14 in
Docker (stock settings, container limited to 1 GB, Docker VM with 10 CPUs and 7.75 GiB
shared with other containers); DuckDB 2.0.0 in process; psycopg 3.3.9. Generating the
ten-million-row table took 87 s in Postgres and 8.5 s in DuckDB.

## Compared with the alternatives

None of these comparisons are benchmarks; reconkit has not been measured against any of
the tools below.

**[data-diff](https://github.com/datafold/data-diff)** (Datafold; archived in 2024) is
where the checksum-bisection approach for cross-database diffs became widely known, and
reconkit borrows it, along with its command line (`reconkit data-diff`). data-diff
supported far more databases than reconkit does, each actually run against the real
engine, and it had years of production use. If you need MySQL, Oracle, Databricks or
Trino today, its code is still the more complete reference, although unmaintained. What
reconkit does differently: one grouped query per level rather than one query per segment,
a finer first pass, tolerances applied in an exact local comparison, an explicit setting
for naive local-time timestamps, and contracts. **[reladiff](https://github.com/erezsh/reladiff)**,
a community fork of data-diff, keeps its engine; it was last updated in August 2025.

**[dbt audit_helper](https://github.com/dbt-labs/dbt-audit-helper)** compares two
relations inside one warehouse with SQL macros. If both tables are in the same warehouse
and you already use dbt, use it: no new tool, the results land in the warehouse, and it
understands dbt models. It cannot compare across databases, which is the case reconkit
exists for. reconkit's same-connection join path overlaps with it and is not better.

**[Great Expectations](https://greatexpectations.io/) and [Soda](https://www.soda.io/)**
are data-quality frameworks: large libraries of checks, profiling, documentation,
alerting, hosted products. For validating one dataset they are far more capable than
reconkit's contract checks, which cover a deliberately small set of rules. They are not
primarily row-level cross-system diff tools. (Soda offers reconciliation checks; check
its current documentation for what they cover and how they are licensed.)

**[datacontract-cli](https://github.com/datacontract/datacontract-cli)** is the fuller
tool for contracts themselves: it lints, tests and diffs contracts across more platforms
and understands more of the standard than reconkit's subset. reconkit's contract support
is there so that one tool and one CI job can answer both "do the copies agree" and "is
the copy what was promised"; if contracts are your main concern, start there.

## Design decisions

Each is written up with the alternatives and costs in [docs/adr/](docs/adr/README.md).

| Decision | Trade-off accepted |
| --- | --- |
| One aggregate query per bisection level, with a fine first pass | Long SQL statements when many buckets mismatch; no early exit inside a level |
| Normalise to canonical text in SQL; checksums select rows, the local comparison decides | Reports show canonical values, not raw ones; tolerances do not reduce rows downloaded |
| Hand-written dialects over plain DB-API; sqlglot only parses SQL in tests | Each engine is manual work; engines without a live test are unproven |
| Range buckets for integer keys, hash buckets otherwise | Hash bucketing cannot use an index: every level is a full scan |
| ODCS-shaped contracts as a checked subset; changes judged from the consumer's side | Unsupported ODCS features are silently not checked; classification errs towards "breaking" |

## Limitations

Things that are not done, not verified, or known to be weak.

**Not verified**

- **Snowflake, BigQuery, Redshift** run only against emulators, never a live account.
  Each is tested end to end on a 400-row table pair with planted differences, against
  DuckDB and PostgreSQL, through its real client library: fakesnow 0.12.1 for Snowflake
  (DuckDB underneath), goccy/bigquery-emulator 0.8.1, and Floci 2.5.0 for Redshift
  (PostgreSQL underneath). An emulator is not the service: Snowflake's hex-to-number
  hash, BigQuery's `MOD` and Redshift's `STRTOL` were replaced in the tests because the
  emulators cannot run them, and every value is computed by the emulator's engine.
  [docs/adapters.md](docs/adapters.md) lists what each one does and does not prove.
  Running them found three bugs, fixed in 0.2.0: string escaping in Snowflake and
  BigQuery, and BigQuery's column lookup.
- The Airflow operator and Dagster helper are tested against stand-ins, not against a
  running scheduler.
- The CI workflow runs lint, type checks, a Python 3.10 to 3.13 test matrix on Linux
  with PostgreSQL 16, and a wheel smoke test, on every push to `main`. The example
  workflow in `examples/github-actions/` has not been run.
- The benchmark was run on one laptop with both databases on localhost. It says nothing
  measured about networked or cloud warehouses.

**Semantics**

- **No snapshot isolation.** Tables that change during a diff can produce spurious
  differences. Filter both sides to a settled window with `where`.
- **Floats** are compared after rounding to `float_precision` decimals. Engines can
  disagree in the last digit when converting a float sitting on a rounding boundary,
  which shows up as a reported difference. Floats are reliable to about 15 significant
  digits in total. NaN and infinities are not handled.
- **Text** is compared byte for byte after optional trailing-whitespace trimming. No
  Unicode normalisation, no case folding, no collation awareness.
- **Unsupported types** (JSON, arrays, binary, time, interval) fall back to each engine's
  own text cast, with a warning. Across engines these will usually differ.
- **Timestamps** are compared to microseconds; finer precision is truncated. Infinite
  timestamps and years outside 0001 to 9999 are not handled.
- **Checksum collisions.** A mismatch is missed if 60-bit sums collide with equal counts
  (about 2^-60 per bucket), or if text moves across the boundary of two adjacent text
  columns that contain the `|` separator. See docs/algorithm.md.
- **Keys.** Range bucketing rejects NULL keys with an error and needs keys that fit a
  signed 64-bit integer. Duplicate keys are only noticed inside buckets that mismatch;
  set `validate_unique_key` to check up front.
- **Sampling** finds a difference with probability equal to the sample rate. A sampled
  match is not a match.

**Performance**

- When most rows differ, bisection is slower than downloading both tables: it checksums
  everything and then downloads everything anyway.
- Hash bucketing (uuid, text or composite non-integer keys) scans the whole table at
  every level. It is sized to finish in three scans per side, but those are full scans.
- Tolerances do not reduce what is downloaded. If many rows differ only within tolerance,
  lower that column's `precision` instead.
- All reported differences are held in memory. Use `limit` for tables that are expected
  to differ widely.
- Contract checks run full-table aggregates, including `COUNT(DISTINCT)` for uniqueness.

**Scope**

- Postgres materialized views are not found (they are absent from
  `information_schema.columns`).
- The contract support is a subset of ODCS; see [docs/contracts.md](docs/contracts.md).
- `where` clauses and contract SQL rules run as written. Treat `reconkit.yml` and
  contract files as code: do not load them from untrusted sources.

## Development

```bash
uv sync
docker compose up -d --wait
uv run ruff format --check . && uv run ruff check . && uv run mypy
uv run pytest --cov
```

The emulator tests need the emulators running and are skipped otherwise:

```bash
docker compose --profile emulators up -d bigquery floci
RECONKIT_REQUIRE_PG=1 RECONKIT_REQUIRE_SNOWFLAKE=1 RECONKIT_REQUIRE_BIGQUERY=1 \
  RECONKIT_REQUIRE_REDSHIFT=1 uv run pytest --cov
```

At 0.2.0 that is 331 tests: 291 that need no database (unit tests, golden SQL snapshots
for all five dialects, property-based tests that compare the diff against a brute-force
oracle on random table pairs, and the data-diff and dbt compatibility tests on DuckDB),
14 against PostgreSQL 16, and 26 against the Snowflake, BigQuery and Redshift emulators.
With all of them running, coverage measured with pytest-cov is 95% of `src/reconkit`
(statements and branches). `mypy --strict` passes with nine targeted `type: ignore`
comments: optional-dependency shims (Airflow, Dagster, `tomli`), untyped Google client
calls, and one decorator. The suite was last run on Python 3.12 (everything) and 3.10
(the tests that need no database), on macOS arm64.

See [CONTRIBUTING.md](CONTRIBUTING.md), [docs/adapters.md](docs/adapters.md) for writing
an adapter, and [CHANGELOG.md](CHANGELOG.md).

## License

MIT. Copyright (c) 2026 Linus Dominic Nathaniel.
