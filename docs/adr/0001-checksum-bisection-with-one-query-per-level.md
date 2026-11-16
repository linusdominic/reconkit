# 0001. Checksum bisection, with one aggregate query per level

Status: accepted

## Context

The tables being reconciled sit in different databases. The cost that matters is not CPU
in either database, which is cheap and scales, but rows crossing the network into a
single Python process: a full download is slow, memory-hungry, and for cloud warehouses
it is billed egress. Most reconciliation runs find few or no differences, so the common
case should transfer almost nothing.

The known technique (used by Datafold's data-diff, now archived, and before it by
pt-table-checksum and rsync) is to compare checksums of key ranges and recurse into
ranges that disagree.

## Decision

Use checksum bisection, but issue **one `GROUP BY bucket` query per level per side**
covering all mismatching buckets, instead of one query per segment.

data-diff, as I understand its design, sent a separate
`SELECT count(*), sum(hash) ... WHERE key BETWEEN a AND b` for every segment at every
level, from a thread pool. With `d` scattered differences and
bisection factor `f` that is on the order of `d * f` queries per level. Each is cheap to
compute but pays a round trip, and on warehouses that bill or queue per query
(BigQuery, Snowflake) a few thousand tiny queries is the wrong shape. A grouped query
returns the same information in one statement and lets the database scan in parallel.

Because one grouped query can return many buckets cheaply, the first pass is also much
finer than a plain 32-way split: by default one bucket per `bisection_threshold` rows,
up to 65,536. See the parameter sweep in the README.

## Consequences

- Query count grows with the number of levels, not with the number of differences: 6 to
  22 statements per diff in the benchmark, both sides and catalogue lookups included.
- Statements grow with the number of mismatching buckets (an `OR` of ranges, or an `IN`
  list). They are chunked at 256 buckets per statement to keep SQL text bounded, so a
  pathological diff does issue more statements.
- A level cannot stop early: it either checksums all mismatching buckets or none.
  `limit` therefore takes effect between fetch batches, not between rows.
- When the tables differ in most rows, bisection does strictly more work than a download:
  it checksums everything and then downloads everything. There is no automatic fallback;
  the report shows rows downloaded so the situation is visible.
- No snapshot isolation across levels. Documented in docs/algorithm.md under
  "Consistency".
