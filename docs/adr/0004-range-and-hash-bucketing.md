# 0004. Two bucketing schemes: key ranges for integers, key hashes for everything else

Status: accepted

## Context

Bisection needs a way to cut the key space into buckets that both databases compute
identically and that can be refined level by level.

Splitting a numeric range is easy. Splitting a range of uuids or strings is not:
arithmetic on them has to be invented (data-diff mapped uuids to integers and
alphanumeric keys through a custom base conversion, a sizeable amount of special-case
code), and string ordering depends on collation, which differs between engines and even between
databases on the same engine.

## Decision

- **Range bucketing** when the first key column is an integer on both sides: equal-width
  ranges over `[min, max]`, widths chosen so each bucket divides exactly into
  `bisection_factor` children. Predicates are ranges on the raw column.
- **Hash bucketing** otherwise: `bucket = hash(key) mod M`, with `M` multiplied by the
  factor per level. The hash is the same 60-bit MD5 slice used for row hashes, over the
  canonical text of all key columns, so it is identical on both engines for any key type
  and for composite keys, with no dependence on collation or ordering.
- `bucketing: auto` picks between them; either can be forced.

## Consequences

- Range bucketing lets the database use the primary-key index for every level after the
  first. This is the fast path and the common case (surrogate integer keys).
- Hash bucketing cannot use an index: every level is a full scan computing a hash per
  row. reconkit therefore sizes the first hash pass to reach the download threshold
  immediately, and a typical hash diff is three scans per side (count, checksum, fetch).
  It is measurably slower than range bucketing (see the benchmark) and that is inherent.
- Range buckets are equal-width, not equal-count. Clustered keys make uneven buckets;
  dense ones split further. Quantile-based splitting would even this out at the cost of
  an extra pass, and was not implemented.
- Integer keys wider than 64 bits (`HUGEINT`, `NUMERIC(38,0)` values beyond bigint) are
  not supported by range bucketing; use `bucketing: hash`.
- Date and timestamp keys use hash bucketing, although range bucketing would be possible
  for them. Not implemented.
- Rows with a NULL key are rejected by range bucketing with an explicit error (a range
  predicate would silently skip them) and handled normally by hash bucketing.
