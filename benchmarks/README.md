# Benchmark

`bench.py` measures reconkit against the simplest correct alternative, on data it
generates itself.

```bash
docker compose up -d --wait
uv run python benchmarks/bench.py --rows 1000000 --diffs 1000 --runs 5 --sweep
uv run python benchmarks/bench.py --rows 10000000 --diffs 1000 --runs 3
```

## What it does

1. Creates the same table in Postgres (`generate_series`) and in a DuckDB file (`range`)
   from deterministic SQL expressions: a `bigint` key and seven payload columns
   (`bigint`, `text`, `numeric(14,2)`, `numeric(8,4)`, `timestamp`, a 32-character `text`,
   `boolean`). Postgres gets a primary key and `VACUUM ANALYZE`.
2. Times reconkit on the identical tables and requires zero differences. This doubles as
   a check that the two generators really produce the same data.
3. Injects differences at seeded random keys: a third deleted from Postgres, a third
   deleted from DuckDB, a third with `amount` changed in DuckDB.
4. Times each strategy and **fails unless it reports exactly the injected keys**, by kind.

## Strategies

- **reconkit (range)**: the default for an integer key.
- **reconkit (hash)**: `bucketing: hash`, which is what a uuid or text key would get.
  Same table, so the comparison isolates the bucketing scheme.
- **baseline (download and compare)**: `SELECT * FROM t ORDER BY id` on both databases,
  streamed in batches of 50,000 (a server-side cursor on Postgres), merge-compared in
  Python by key with tuple equality. Constant memory. This is a fair version of "pull
  both and compare", not a strawman, but it is single-threaded and does no
  normalisation, which works here only because the generated types happen to map to
  equal Python values in both drivers.

## What is measured

- **Wall time** of one run on fresh connections, connection setup and catalogue lookups
  included. The table reports the median; the JSON keeps min and max.
- **Queries**: statements issued, both sides added together.
- **Rows transferred**: every row any query returned, both sides, checksum rows included.
- **Table rows downloaded**: of those, actual table rows fetched for local comparison.

## Reading the numbers

- Both databases are on localhost. Transferring a row costs almost nothing here, which
  flatters the baseline. Over a real network, and especially from a warehouse that bills
  for scanned or egressed data, rows transferred is the number that matters.
- Postgres runs in a Docker container limited to 1 GB of memory, with stock settings.
- reconkit's time is dominated by MD5 over every row in the first pass. That cost is paid
  inside the databases and parallelises there.
- A thousand differences spread uniformly at random is close to the worst case for
  bisection at a given difference count: almost every difference lands in its own bucket.
  Real discrepancies tend to cluster (a failed batch, a date range).

Results from the run described in the top-level README are in `results/`.
