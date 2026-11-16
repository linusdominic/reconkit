# Data contracts

A contract is a YAML file that states what a table promises: its columns, their types,
which may be NULL, which are unique, and quality rules such as freshness and row-count
bounds. reconkit does two things with it:

- `reconkit contract check CONTRACT --connection NAME` verifies a live table against it.
- `reconkit contract diff OLD NEW` classifies the changes between two versions as
  breaking or non-breaking, for use as a pull-request gate.

## File shape

The shape follows the [Open Data Contract Standard](https://bitol-io.github.io/open-data-contract-standard/)
(ODCS) v3: a top-level `schema` list of objects, each with `properties` and `quality`.
reconkit reads the subset below and ignores every other key, so a full ODCS document
(team, pricing, SLA, servers, ...) loads without changes. It is a reader of ODCS-shaped
files, not a validator of ODCS conformance.

```yaml
apiVersion: v3.1.0
kind: DataContract
name: fct_loans
version: 1.2.0

schema:
  - name: fct_loans
    physicalName: analytics.fct_loans      # table to check; defaults to name
    properties:
      - name: loan_id
        logicalType: integer               # string | integer | number | boolean | date | timestamp
        physicalType: BIGINT               # optional; parsed with the connection's dialect
        primaryKey: true                   # implies a uniqueness check
        required: true                     # no NULLs
      - name: customer_id
        logicalType: integer
        relationships:
          - type: foreignKey
            to: analytics.dim_customers.customer_id   # no orphans allowed
      - name: loan_status
        logicalType: string
        quality:
          - name: known_status
            metric: invalidValues
            arguments: {validValues: [APPROVED, DISBURSED, REJECTED]}
            mustBe: 0
      - name: _loaded_at
        logicalType: timestamp
        quality:
          - metric: freshness              # reconkit extension, see below
            unit: hours
            mustBeLessThan: 24
    quality:
      - metric: rowCount
        mustBeBetween: [1000, 10000000]
      - name: disbursed_loans_have_a_disbursement_time
        type: sql
        query: SELECT COUNT(*) FROM ${object} WHERE loan_status = 'DISBURSED' AND disbursed_at IS NULL
        mustBe: 0
```

## What is checked

| Declaration | Check | Query cost |
| --- | --- | --- |
| object | the table exists | catalogue lookup |
| property | the column exists; columns not in the contract are reported as warnings | same lookup |
| `logicalType` | the column's type family is compatible (`number` accepts integer, decimal and float; `date` accepts date and timestamp; `object`/`array` are skipped) | none |
| `physicalType` | type family, timezone-awareness and decimal scale match | none |
| `required: true` | zero NULLs | batched |
| `unique: true`, single `primaryKey` | zero duplicated values | batched |
| several `primaryKey` properties | zero duplicated key combinations | one query |
| `metric: nullValues` / `missingValues` / `duplicateValues` / `invalidValues` | count (or `unit: percent` of rows) against the operator | batched |
| `metric: rowCount` | row count against the operator | batched |
| `metric: freshness` | age of `MAX(column)` against the operator | batched |
| `type: sql` | first column of the first row of your query against the operator | one query each |
| `relationships` | rows whose value has no match in the referenced column | one query each |
| `type: text`, `type: custom` | not machine-checked; reported as skipped | none |

"Batched" means all such rules for a table are evaluated in a single aggregate query.

Operators are ODCS's: `mustBe`, `mustNotBe`, `mustBeGreaterThan`,
`mustBeGreaterOrEqualTo`, `mustBeLessThan`, `mustBeLessOrEqualTo`, `mustBeBetween`,
`mustNotBeBetween`. A rule without an operator is reported as an error rather than
silently passing.

In `type: sql` queries, `${object}` (also `{object}`, `${table}`) is replaced with the
table and `${property}` (also `${column}`) with the quoted column name. The query runs
as written, in the target database's dialect, with the connection's privileges.

### Differences from ODCS

- `freshness` is not an ODCS library metric. ODCS expresses freshness under
  `slaProperties`; reconkit does not read that section. Units: `seconds`, `minutes`,
  `hours` (default), `days`. Timezone-naive timestamps are assumed to be UTC.
- v3.0 rule names (`rule: duplicateCount`, `rule: nullCount`) are accepted as aliases of
  the v3.1 metric names.
- `invalidValues` supports `arguments.validValues` only (no `pattern`), and
  `missingValues` treats NULL plus `arguments.missingValues` (default `['']`) as missing.
- Rule `severity`, `dimension`, `scheduler` and `businessImpact` are ignored: a rule
  either passes or fails.

## Classifying changes

`reconkit contract diff old.yml new.yml` takes the **consumer's** point of view: a change
is breaking when something a consumer could rely on under the old contract is no longer
promised.

| Change | Classification |
| --- | --- |
| schema object or property removed | breaking |
| schema object or property added | non-breaking |
| `physicalName` changed | breaking |
| `logicalType` changed | breaking |
| `physicalType` changed (beyond case and spacing) | breaking; a pure widening still needs a human to say so |
| `required` or `unique` dropped | breaking |
| `required` or `unique` added | non-breaking |
| `primaryKey` changed either way | breaking |
| relationship removed / added | breaking / non-breaking |
| quality rule removed / added | breaking / non-breaking |
| threshold changed, new accepted interval inside the old one | non-breaking (tightened) |
| threshold changed otherwise | breaking (loosened or moved) |
| value added to `validValues` | breaking (consumers' enums see a new value) |
| value removed from `validValues` | non-breaking |
| rule's metric, query or unit changed | breaking (cannot be compared) |

Rules are paired between versions by `name`, or by metric when unnamed. Name your rules
if a property carries more than one rule with the same metric.

Tightening a guarantee is non-breaking for consumers but may be untrue of the data. That
is what `contract check` is for; run both in CI.

Exit codes: 0 when there are no breaking changes, 1 when there are. With
`--require-major-bump`, breaking changes are accepted if the contract's major version
increased. `--format markdown` produces a table suitable for a pull-request comment or
`$GITHUB_STEP_SUMMARY`; see `examples/github-actions/reconcile.yml`.
