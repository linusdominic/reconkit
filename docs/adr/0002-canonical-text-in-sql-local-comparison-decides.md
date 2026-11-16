# 0002. Canonical text in SQL; checksums select rows, the local comparison decides

Status: accepted

## Context

Two questions had to be answered together.

1. Checksums from two engines are only comparable if both hash identical bytes for the
   same logical value. `numeric(14,2)` against `DOUBLE`, `timestamptz` against a naive
   local-time `TIMESTAMP`, `CHAR(6)` against `VARCHAR`: none of these agree by default.
2. Business rules such as "amounts may differ by a cent" or "timestamps within a second"
   cannot be expressed in a checksum at all. Rounding before hashing looks like it works
   but fails at bucket edges: 0.4999 and 0.5001 round to different values.

A related practical problem is that drivers return different Python types for the same
SQL type (`Decimal` or `float`, aware or naive `datetime`, `UUID` or `str`), so comparing
fetched rows natively needs a second normalisation layer in Python that must agree with
the SQL one.

## Decision

- Every compared column is converted **in SQL** to one canonical text form per column
  pair, chosen from both sides' catalogue types. Rows are hashed from that text, and rows
  are fetched as that text. There is one normalisation path, not two.
- NULL is encoded out of band (`N`, or `V` + text), so it cannot collide with a value.
- The row hash is the last 60 bits of MD5 over the encoded columns; the bucket checksum
  is the pair (row count, sum of row hashes).
- **Checksums only choose which rows are downloaded.** Whether a row is a difference is
  decided locally on the canonical values, where tolerances are applied exactly.

## Alternatives considered

- *Fetch native values and normalise in Python.* Two implementations of the same rules,
  one per language, multiplied by drivers. Rejected.
- *Engine-native hashes* (`hashtext`, `HASH()`, `FARM_FINGERPRINT`). Faster than MD5 but
  each engine has a different function. MD5 is the common denominator.
- *XOR instead of sum.* Equivalent strength, but there is no portable `BIT_XOR`
  aggregate. Sum of 60-bit values in a 128-bit accumulator exists everywhere.
- *Length-prefixed fields instead of a separator.* Removes the one structural collision
  (text shifting across a `|` between adjacent text columns) at the cost of a `LENGTH`
  call per column whose character semantics also differ by engine. Kept the separator and
  documented the case.

## Consequences

- Reports show canonical values (`2024-03-10 01:30:00.000000`, `1234.50`), not the
  engines' native renderings. For reconciliation that is usually what you want, since the
  two are directly comparable, but it is not the raw stored value.
- Tolerances are exact but do not reduce the rows downloaded: rows inside tolerance still
  have different checksums. Coarser `precision` is the tool for systematic noise.
- Text casts and MD5 on every row are the CPU cost of the method, paid inside the
  databases.
- Canonical forms are only as portable as the SQL functions behind them. PostgreSQL and
  DuckDB are verified against each other by a type-matrix integration test and a
  property-based test. Other dialects are not (see docs/adapters.md).
- Floats are compared after rounding to `float_precision` decimals. Engines disagree in
  the last binary digit when converting floats to decimal, so a value that sits exactly
  on a rounding boundary can be reported as a difference. NaN and infinities are not
  handled.
