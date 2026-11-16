# 0005. Contracts in the ODCS shape, as a checked subset, judged from the consumer's side

Status: accepted

## Context

Reconciliation answers "do these two copies agree?". It does not answer "is either of
them what downstream users were promised?". A table can be perfectly in sync with its
source and still have lost a column. The second question needs a written contract, and a
contract file needs a format.

Inventing a format would be the easy path and the wrong one: teams that already write
contracts mostly use the Open Data Contract Standard (ODCS, a Linux Foundation / Bitol
project) or tools that read it.

## Decision

- The contract file has the **shape of ODCS v3**: `schema[].properties[]` with
  `logicalType`, `physicalType`, `required`, `unique`, `primaryKey`, and `quality[]`
  rules with ODCS's operator names. Unknown keys are ignored, so an existing ODCS
  document loads unchanged.
- reconkit implements the **subset it can verify against a database** and says so. It is
  not a conformance validator, and it adds one metric ODCS does not have as a library
  rule (`freshness`). docs/contracts.md lists every difference.
- `contract diff` classifies changes from the **consumer's** side: a change is breaking
  when a guarantee is removed or weakened. Tightening is non-breaking. Changes whose
  direction cannot be determined mechanically (a rewritten SQL rule, a physical type
  change) are classified as breaking, so the gate fails closed and a person decides.
- A breaking change can be allowed through CI only with a major version bump
  (`--require-major-bump`), tying the classification to semantic versioning.

## Alternatives considered

- *Great Expectations suites or Soda checks as the contract.* Both are richer check
  languages. Neither is a schema-plus-guarantees document that a consumer can read, and
  neither has a notion of diffing two versions for compatibility.
- *JSON Schema / Avro compatibility rules.* Well-defined for message schemas, but they
  cover structure only, not freshness or row counts.
- *Producer-side classification* (adding a required column is breaking for the writer).
  Useful, but a different question; `contract check` against the producer's table already
  answers whether the producer meets the new contract.

## Consequences

- An unsupported ODCS feature is silently not checked, because unknown keys are ignored.
  That is the price of loading real ODCS files unchanged. `type: text` and `type: custom`
  rules are reported as skipped so they are at least visible.
- Classification is conservative: some reported "breaking" changes are harmless (widening
  `INTEGER` to `BIGINT`). The alternative, a per-engine type-widening lattice, was judged
  not worth its maintenance cost.
- Contract checks run aggregate queries over the whole table (batched into one statement
  where possible). On very large tables, uniqueness checks (`COUNT(DISTINCT ...)`) are
  the expensive part; there is no sampling for contract checks.
