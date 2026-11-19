# Migrating from data-diff

[data-diff](https://github.com/datafold/data-diff) was archived by Datafold in May 2024.
Its main community fork, [reladiff](https://github.com/erezsh/reladiff), was last updated
in August 2025. reconkit uses the same checksum-bisection idea, and
`reconkit data-diff` accepts data-diff's command line, so most scripts only need the
command name changed:

```sh
# before
data-diff postgresql://app@oltp/lending loans \
          "snowflake://svc@acct/ANALYTICS/PUBLIC?warehouse=WH" FCT_LOANS \
          -k loan_id -c principal_amount -c "status%" --stats

# after
reconkit data-diff postgresql://app@oltp/lending loans \
          "snowflake://svc@acct/ANALYTICS/PUBLIC?warehouse=WH" FCT_LOANS \
          -k loan_id -c principal_amount -c "status%" --stats
```

Install, from source (reconkit is not on PyPI yet), with the extras for your databases:

```sh
pip install "reconkit[postgres,snowflake] @ git+https://github.com/linusdominic/reconkit"
```

Extras: `postgres`, `duckdb`, `snowflake`, `bigquery`, `redshift`.

## What carries over

| data-diff | `reconkit data-diff` |
| --- | --- |
| `DB1_URI TABLE1 DB2_URI TABLE2` | Same. URIs for `postgresql://`, `redshift://`, `snowflake://` (including `?warehouse=`, `&role=`, `&key=`), `bigquery://project/dataset` and `duckdb://` |
| `-k/--key-columns` (default `id`), `-t/--update-column`, `-c/--columns` with `%` wildcards | Same. As in data-diff, only the key, the update column and `--columns` are compared; pass `-c %` to compare every shared column |
| `-w/--where`, `--min-age`, `--max-age` | Same, with the same time units (`s`, `min`, `h`, `d`, `w`, `mon` = 30 days, `y` = 365 days). The cut-off is computed in UTC |
| `-l/--limit` | Same: counts printed lines, and a changed row prints two |
| `-a/--algorithm auto\|joindiff\|hashdiff` | Mapped to reconkit's `auto`, `join` and `bisect` strategies. `joindiff` still needs both tables in one database |
| `--bisection-factor`, `--bisection-threshold` | Same meaning |
| Output: `- values` / `+ values`, `--json` lines, `-s/--stats` text and `--stats --json` | Same formats and field names (`rows_A`, `exclusive_A`, `updated`, ...). `Extra-Info` lists reconkit's own counters |
| `--conf FILE --run NAME` (TOML: `[database.*]`, `[run.default]`, `[run.NAME]`, `${VAR}`) | Same. Values given on the command line win over the file |
| `--dbt` with `--dbt-project-dir`, `--dbt-profiles-dir`, `--select`, `--state`, `-pd/--prod-database`, `-ps/--prod-schema` | Same; see [dbt](#dbt) |
| `--case-sensitive` | Same |

## What is different

- **Exit codes.** data-diff logged errors and exited 0. reconkit exits 2 for a bad
  argument or configuration and 3 when a query fails. Differences still exit 0, as
  before, unless you add `--fail-on-diff`, which exits 1 when anything differs. That
  makes it usable as a CI gate without parsing output.
- **Printed values** are reconkit's canonical text forms: timestamps with six fractional
  digits, decimals at the column's scale, floats rounded to six decimal places, NULL as
  `None`. They are what reconkit compares, so two engines print the same text for the
  same value. Formatting can differ from what data-diff printed for the same row, for
  floats especially, so check any script that parses values rather than signs.
- **`-s` is only `--stats`.** In data-diff, `-s` was declared for `--stats`, `--select`
  and `--state` at once. Use the long forms for the dbt options.
- **`-j/--threads`** is accepted and ignored.
- **`--assume-unique-key`** is accepted and changes nothing: reconkit never fails on
  duplicate keys; it reports them as a warning.
- **`-d/--debug`** is the same as `-v/--verbose`, which prints the columns compared and
  the duration to stderr. It does not print SQL.
- **Nothing is sent anywhere.** `--no-tracking` is accepted and has no effect.

## Not supported

These are refused with an error rather than silently ignored:

- `-m/--materialize-to-table`, `--materialize-all-rows`, `--sample-exclusive-rows` and
  `--table-write-limit` (join-diff output tables);
- `--cloud` (Datafold's hosted service) and `-i/--interactive`;
- the drivers reconkit has no adapter for: MySQL, Oracle, Presto, Trino, Databricks,
  ClickHouse, Vertica and SQL Server. [docs/adapters.md](adapters.md) describes how to add
  one.

## Beyond data-diff

The same comparison is available as a named, versioned check in `reconkit.yml` (`reconkit
run`), with per-column tolerances, column renames, timezone handling for naive
timestamps, sampling, JUnit and HTML reports, audit tables, and Airflow and Dagster
integrations. See the [README](../README.md#using-it). A data-diff run translates
directly:

```yaml
connections:
  oltp: {type: postgres, url: "postgresql://app:${OLTP_PASSWORD}@oltp/lending"}
  warehouse: {type: snowflake, account: acct, user: svc, password: "${SF_PASSWORD}",
              database: ANALYTICS, schema: PUBLIC, warehouse: WH}
checks:
  loans:
    left: {connection: oltp, table: loans}
    right: {connection: warehouse, table: FCT_LOANS}
    key: loan_id
    columns: [principal_amount, status]
```

The difference in defaults: a `reconkit.yml` check compares every shared column unless
`columns` is given, while `reconkit data-diff` keeps data-diff's keys-only default.

## dbt

`reconkit data-diff --dbt` compares each model's development build with its production
copy in the same warehouse, as data-diff did. It reads the project's files directly, so
dbt does not need to be installed alongside it:

- `dbt_project.yml`: the profile, `target-path`, and data-diff's settings under
  `vars: data_diff:` (`prod_database`, `prod_schema`, `prod_custom_schema` with its
  `<custom_schema>` placeholder).
- `profiles.yml`, found the way dbt finds it (`--dbt-profiles-dir`, `DBT_PROFILES_DIR`,
  the project directory, `~/.dbt`), with the profile's target or `DBT_TARGET`.
  `{{ env_var('NAME', 'default') }}` is resolved, with `| int` and `| as_number`; any
  other Jinja is refused. Supported adapter types: postgres, redshift (password
  authentication), snowflake, bigquery (`oauth` or `service-account`) and duckdb.
- `target/manifest.json`, and `target/run_results.json` to choose the models the last
  run built successfully, unless `--select` is given. `--state PATH` takes production
  locations from a production manifest instead of from the variables.

Primary keys come from the same places as in data-diff, in this order: model `meta`
`primary-key` (a column name or a list), column `meta` or `tags` `primary-key`, then
`unique` and `dbt_utils.unique_combination_of_columns` tests. A model without one is
skipped with a message. Per-model `meta: datafold: datadiff:` settings (`filter`,
`include_columns`, `exclude_columns`) are honoured, and `meta: reconkit:` is accepted as
an alias. As in data-diff, a column whose type differs between production and dev is
reported under "Type changed" and left out of the value comparison.

`--select` supports a subset of dbt's selection syntax, evaluated against the manifest:
model names with `*` wildcards, `tag:`, `path:`, `fqn:`, the graph operators `+model`,
`model+`, `2+model` and `model+1`, spaces for union and commas for intersection. Other
methods (`state:modified`, `source:`, `@`, `config.`...) are refused with a message.

### How the compatibility was checked

data-diff's own test suite contains a dbt project (`jaffle_shop`, with development and
production schemas in DuckDB) and the output its `--dbt` mode is expected to print for
it. That project is reproduced in
[tests/fixtures/data_diff_jaffle_shop](../tests/fixtures/data_diff_jaffle_shop/NOTICE.md),
and `tests/unit/test_compat_dbt.py::test_matches_data_diffs_own_expected_output` runs
`reconkit data-diff --dbt` on it and checks the output against data-diff's expected text,
compared the way data-diff's test compares it (whitespace removed). It passes: the same
row counts (10 in production, 11 in dev; 2 added, 1 removed, 9 different), the same
per-column counts, the same "Type changed" and added/removed column lines, and "No row
differences" for the other four models.

What was **not** checked:

- reconkit was not run side by side with the data-diff executable. The comparison above
  is against the output data-diff's tests expect, not a fresh run of data-diff.
- The non-dbt output formats follow data-diff's source code (`__main__.py`,
  `diff_tables.py`); data-diff's own CLI tests only count output lines, so there is no
  recorded data-diff output to compare them with.
- The dbt fixture was generated with dbt 1.7. Manifests from newer dbt versions are read
  the same way (`meta` is taken from both the node and its `config`), but no newer
  manifest is in the test suite.
