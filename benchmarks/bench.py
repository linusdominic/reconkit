"""Reproducible benchmark: reconkit bisection vs. downloading both tables.

Generates the same N-row table in Postgres and in a DuckDB file from deterministic SQL
expressions, checks the two copies reconcile cleanly, injects a known set of differences,
and then times:

* reconkit with range bucketing (the default for an integer key)
* reconkit with hash bucketing (what a uuid/text key would get)
* a baseline that streams ``SELECT * ... ORDER BY id`` from both databases and
  merge-compares the rows in Python

Every strategy must report exactly the injected differences or the script fails.

    docker compose up -d --wait
    uv run python benchmarks/bench.py --rows 1000000 --diffs 1000

Results are printed as a Markdown table and written to benchmarks/results/.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import psycopg

from reconkit import DiffOptions, DiffResult, TableSpec, diff_tables
from reconkit.adapters.duckdb import from_connection
from reconkit.adapters.postgres import connect as pg_connect

HERE = Path(__file__).parent
PG_URL = os.environ.get(
    "RECONKIT_BENCH_PG_URL", "postgresql://reconkit:reconkit@localhost:55439/reconkit"
)

PG_SELECT = """
    SELECT g::bigint AS id,
           ((g * 7919) % 1000003)::bigint AS customer_id,
           (ARRAY['OPEN','CLOSED','PENDING','REJECTED'])[(g % 4) + 1] AS status,
           (((g * 31337) % 100000000)::numeric / 100)::numeric(14,2) AS amount,
           (((g * 13) % 100000)::numeric / 10000)::numeric(8,4) AS rate,
           TIMESTAMP '2020-01-01 00:00:00' + ((g * 37) % 100000000) * INTERVAL '1 second'
               AS created_at,
           md5(g::text) AS note,
           (g % 3 = 0) AS flag
    FROM generate_series(1::bigint, {rows}) g
"""
DUCK_SELECT = """
    SELECT g::BIGINT AS id,
           ((g * 7919) % 1000003)::BIGINT AS customer_id,
           ['OPEN','CLOSED','PENDING','REJECTED'][(g % 4) + 1] AS status,
           (((g * 31337) % 100000000)::DECIMAL(18,2) / 100)::DECIMAL(14,2) AS amount,
           (((g * 13) % 100000)::DECIMAL(18,4) / 10000)::DECIMAL(8,4) AS rate,
           TIMESTAMP '2020-01-01 00:00:00' + to_seconds((g * 37) % 100000000) AS created_at,
           md5(g::VARCHAR) AS note,
           (g % 3 = 0) AS flag
    FROM range(1, {rows} + 1) t(g)
"""


def generate(
    pg: psycopg.Connection[Any], duck: duckdb.DuckDBPyConnection, table: str, rows: int
) -> dict[str, float]:
    started = time.perf_counter()
    pg.execute(f"DROP TABLE IF EXISTS {table}")
    pg.execute(f"CREATE TABLE {table} AS {PG_SELECT.format(rows=rows)}")
    pg.execute(f"ALTER TABLE {table} ADD PRIMARY KEY (id)")
    pg.execute(f"VACUUM ANALYZE {table}")
    pg_seconds = time.perf_counter() - started
    started = time.perf_counter()
    duck.execute(f"DROP TABLE IF EXISTS {table}")
    duck.execute(f"CREATE TABLE {table} AS {DUCK_SELECT.format(rows=rows)}")
    return {"postgres": pg_seconds, "duckdb": time.perf_counter() - started}


def inject(
    pg: psycopg.Connection[Any],
    duck: duckdb.DuckDBPyConnection,
    table: str,
    rows: int,
    diffs: int,
    seed: int,
) -> dict[str, set[int]]:
    """Delete a third from Postgres, delete a third from DuckDB, change a third in DuckDB."""
    ids = random.Random(seed).sample(range(1, rows + 1), diffs)
    third = diffs // 3
    missing_left, missing_right, changed = ids[:third], ids[third : 2 * third], ids[2 * third :]
    pg.execute(f"DELETE FROM {table} WHERE id = ANY(%s)", (missing_left,))
    duck.execute(f"DELETE FROM {table} WHERE id IN ({', '.join(map(str, missing_right))})")
    duck.execute(
        f"UPDATE {table} SET amount = amount + 1 WHERE id IN ({', '.join(map(str, changed))})"
    )
    pg.execute(f"ANALYZE {table}")
    return {
        "missing_in_left": set(missing_left),
        "missing_in_right": set(missing_right),
        "changed": set(changed),
    }


def found(result: DiffResult) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {
        "missing_in_left": set(),
        "missing_in_right": set(),
        "changed": set(),
    }
    for d in result.diffs:
        out[d.kind].add(int(d.key[0]))
    return out


def run_reconkit(table: str, duck_path: Path, **options: Any) -> dict[str, Any]:
    """One timed reconkit run on fresh connections. Connection time is included."""
    started = time.perf_counter()
    left = pg_connect({"url": PG_URL}, name="postgres")
    right = from_connection(duckdb.connect(str(duck_path), read_only=True), name="duckdb")
    try:
        result = diff_tables(
            TableSpec(left, table), TableSpec(right, table), ["id"], options=DiffOptions(**options)
        )
    finally:
        left.close()
        right.close()
    return {
        "seconds": time.perf_counter() - started,
        "queries": result.left.queries + result.right.queries,
        "rows_transferred": result.left.rows_returned + result.right.rows_returned,
        "rows_downloaded": result.left.rows_fetched + result.right.rows_fetched,
        "levels": result.levels,
        "found": found(result),
    }


def run_baseline(table: str, duck_path: Path, batch: int = 50_000) -> dict[str, Any]:
    """Stream both tables ordered by key and merge-compare them in Python."""
    started = time.perf_counter()
    out: dict[str, set[int]] = {
        "missing_in_left": set(),
        "missing_in_right": set(),
        "changed": set(),
    }
    transferred = 0
    sql = f"SELECT * FROM {table} ORDER BY id"
    with (
        psycopg.connect(PG_URL) as pg,
        pg.cursor(name="baseline") as left,
        duckdb.connect(str(duck_path), read_only=True) as duck,
    ):
        left.itersize = batch
        left.execute(sql)
        right = duck.execute(sql)

        def stream(fetch: Callable[[int], list[tuple[Any, ...]]]) -> Any:
            nonlocal transferred
            while chunk := fetch(batch):
                transferred += len(chunk)
                yield from chunk

        lit, rit = stream(left.fetchmany), stream(right.fetchmany)
        lrow, rrow = next(lit, None), next(rit, None)
        while lrow is not None or rrow is not None:
            if rrow is None or (lrow is not None and lrow[0] < rrow[0]):
                assert lrow is not None
                out["missing_in_right"].add(lrow[0])
                lrow = next(lit, None)
            elif lrow is None or rrow[0] < lrow[0]:
                out["missing_in_left"].add(rrow[0])
                rrow = next(rit, None)
            else:
                if lrow != rrow:
                    out["changed"].add(lrow[0])
                lrow, rrow = next(lit, None), next(rit, None)
    return {
        "seconds": time.perf_counter() - started,
        "queries": 2,
        "rows_transferred": transferred,
        "rows_downloaded": transferred,
        "levels": None,
        "found": out,
    }


def measure(
    label: str, fn: Callable[[], dict[str, Any]], runs: int, expected: dict[str, set[int]]
) -> dict[str, Any]:
    samples = []
    for _ in range(runs):
        sample = fn()
        if sample.pop("found") != expected:
            raise SystemExit(f"{label}: reported differences do not match the injected ones")
        samples.append(sample)
    times = [s["seconds"] for s in samples]
    row = {
        **samples[0],
        "strategy": label,
        "seconds": statistics.median(times),
        "seconds_min": min(times),
        "seconds_max": max(times),
        "runs": runs,
    }
    print(
        f"  {label:34} {row['seconds']:8.2f}s  (min {min(times):.2f}, max {max(times):.2f})  "
        f"{row['queries']:4} queries  {row['rows_transferred']:>12,} rows transferred",
        flush=True,
    )
    return row


def machine(pg: psycopg.Connection[Any]) -> dict[str, str]:
    def sh(*cmd: str) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return "unknown"

    row = pg.execute("SELECT version()").fetchone()
    return {
        "cpu": sh("sysctl", "-n", "machdep.cpu.brand_string")
        if platform.system() == "Darwin"
        else platform.processor(),
        "cores": str(os.cpu_count()),
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": platform.python_version(),
        "postgres": str(row[0]).split(" on ")[0] if row else "unknown",
        "duckdb": duckdb.__version__,
        "psycopg": psycopg.__version__,
        "docker": sh("docker", "info", "--format", "{{.NCPU}} CPUs, {{.MemTotal}} bytes RAM in VM"),
    }


SWEEP: list[dict[str, int | None]] = [
    # a coarse first pass with a large download threshold (data-diff's defaults)
    {"bisection_factor": 32, "bisection_threshold": 16384, "initial_buckets": 32},
    {"bisection_factor": 32, "bisection_threshold": 1024, "initial_buckets": 32},
    {"bisection_factor": 32, "bisection_threshold": 64, "initial_buckets": 32},
    {"bisection_factor": 32, "bisection_threshold": 64, "initial_buckets": 1024},
    {"bisection_factor": 32, "bisection_threshold": 16, "initial_buckets": None},
    {"bisection_factor": 32, "bisection_threshold": 64, "initial_buckets": None},  # default
    {"bisection_factor": 32, "bisection_threshold": 256, "initial_buckets": None},
    {"bisection_factor": 8, "bisection_threshold": 64, "initial_buckets": None},
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--diffs", type=int, default=1000, help="differing rows to inject")
    parser.add_argument("--runs", type=int, default=3, help="timed runs per strategy (median)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-baseline", action="store_true")
    parser.add_argument("--sweep", action="store_true", help="also time parameter variations")
    parser.add_argument("--keep", action="store_true", help="keep the generated tables")
    args = parser.parse_args()

    table = f"bench_{args.rows}"
    duck_path = HERE / "data" / f"{table}.duckdb"
    duck_path.parent.mkdir(exist_ok=True)
    duck_path.unlink(missing_ok=True)
    results: list[dict[str, Any]] = []
    none: dict[str, set[int]] = {
        "missing_in_left": set(),
        "missing_in_right": set(),
        "changed": set(),
    }

    with psycopg.connect(PG_URL, autocommit=True) as pg:
        info = machine(pg)
        print(f"generating {args.rows:,} rows in each database ...", flush=True)
        with duckdb.connect(str(duck_path)) as duck:
            gen = generate(pg, duck, table, args.rows)
        print(f"  postgres {gen['postgres']:.1f}s, duckdb {gen['duckdb']:.1f}s")

        print("identical tables:", flush=True)
        results.append(
            {
                "scenario": "identical",
                **measure(
                    "reconkit (range)", lambda: run_reconkit(table, duck_path), args.runs, none
                ),
            }
        )
        results.append(
            {
                "scenario": "identical",
                **measure(
                    "reconkit (hash)",
                    lambda: run_reconkit(table, duck_path, bucketing="hash"),
                    args.runs,
                    none,
                ),
            }
        )

        with duckdb.connect(str(duck_path)) as duck:
            expected = inject(pg, duck, table, args.rows, args.diffs, args.seed)
        scenario = f"{args.diffs:,} differing rows"
        print(f"{scenario}:", flush=True)
        results.append(
            {
                "scenario": scenario,
                **measure(
                    "reconkit (range)", lambda: run_reconkit(table, duck_path), args.runs, expected
                ),
            }
        )
        results.append(
            {
                "scenario": scenario,
                **measure(
                    "reconkit (hash)",
                    lambda: run_reconkit(table, duck_path, bucketing="hash"),
                    args.runs,
                    expected,
                ),
            }
        )
        if not args.no_baseline:
            results.append(
                {
                    "scenario": scenario,
                    **measure(
                        "baseline (download and compare)",
                        lambda: run_baseline(table, duck_path),
                        args.runs,
                        expected,
                    ),
                }
            )
        sweep: list[dict[str, Any]] = []
        if args.sweep:
            print("parameter sweep (range bucketing):", flush=True)
            for params in SWEEP:
                label = "f={bisection_factor} t={bisection_threshold} i={initial_buckets}".format(
                    **params
                ).replace("None", "auto")
                sweep.append(
                    {
                        **params,
                        **measure(
                            label,
                            lambda p=params: run_reconkit(table, duck_path, **p),
                            args.runs,
                            expected,
                        ),
                    }
                )
        if not args.keep:
            pg.execute(f"DROP TABLE {table}")
    if not args.keep:
        duck_path.unlink(missing_ok=True)

    lines = [
        f"### {args.rows:,} rows, 8 columns, {args.diffs:,} injected differences "
        f"(median of {args.runs} runs)",
        "",
        "| Scenario | Strategy | Wall time | Queries | Rows transferred | Table rows downloaded |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    lines += [
        f"| {r['scenario']} | {r['strategy']} | {r['seconds']:.2f} s | {r['queries']} | "
        f"{r['rows_transferred']:,} | {r['rows_downloaded']:,} |"
        for r in results
    ]
    if sweep:
        lines += [
            "",
            "| factor | threshold | initial buckets | Wall time | Queries | "
            "Rows transferred | Levels |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        lines += [
            f"| {s['bisection_factor']} | {s['bisection_threshold']} | "
            f"{s['initial_buckets'] or 'auto'} | "
            f"{s['seconds']:.2f} s | {s['queries']} | {s['rows_transferred']:,} | {s['levels']} |"
            for s in sweep
        ]
    lines += ["", "Machine: " + "; ".join(f"{k}: {v}" for k, v in info.items())]
    markdown = "\n".join(lines) + "\n"
    print("\n" + markdown)
    out = HERE / "results" / f"{table}_{args.diffs}"
    out.with_suffix(".md").write_text(markdown)
    out.with_suffix(".json").write_text(
        json.dumps(
            {
                "rows": args.rows,
                "diffs": args.diffs,
                "runs": args.runs,
                "generation_seconds": gen,
                "machine": info,
                "results": results,
                "sweep": sweep,
            },
            indent=2,
        )
    )
    print(f"written: {out.with_suffix('.md')} and {out.with_suffix('.json')}")


if __name__ == "__main__":
    main()
