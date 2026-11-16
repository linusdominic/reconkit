# Architecture decision records

Short records of decisions that shaped the design, with the alternatives that were
rejected and what each decision costs. A decision that is later reversed gets a new
record that supersedes the old one.

| # | Decision |
| --- | --- |
| [0001](0001-checksum-bisection-with-one-query-per-level.md) | Checksum bisection, with one aggregate query per level rather than one per segment |
| [0002](0002-canonical-text-in-sql-local-comparison-decides.md) | Normalise to canonical text in SQL; checksums select rows, the local comparison decides |
| [0003](0003-hand-written-dialects-over-dbapi.md) | Hand-written SQL dialects over DB-API; sqlglot only as a test-time parser |
| [0004](0004-range-and-hash-bucketing.md) | Two bucketing schemes: key ranges for integer keys, key hashes for everything else |
| [0005](0005-odcs-shaped-contracts-consumer-view.md) | Contracts in the ODCS shape, as a checked subset; breaking changes judged from the consumer's side |
