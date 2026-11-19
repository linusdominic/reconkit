"""Seed the data-diff migration example.

Creates ``servicing.payments`` in Postgres and a copy, ``payments``, in a local DuckDB
file, the way a replication job would. Then plants a known set of differences in the
copy and prints them, so the output of ``reconkit data-diff`` can be checked against it:

* rows deleted from the copy (missing in B),
* rows that exist only in the copy (missing in A),
* payments whose amount was changed,
* payments whose status was changed.

Usage: python seed.py [--rows 1000000] [--seed 7]
"""

from __future__ import annotations

import argparse
import os
import random
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import psycopg

HERE = Path(__file__).parent
PG_URL = os.environ.get(
    "RECONKIT_EXAMPLE_PG_URL", "postgresql://reconkit:reconkit@localhost:55439/reconkit"
)
DDL = (
    "payment_id BIGINT, loan_id BIGINT, amount NUMERIC(12,2), status VARCHAR(12), paid_at TIMESTAMP"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    start = datetime(2025, 1, 1)
    statuses = ["SETTLED"] * 18 + ["RETURNED", "PENDING"]

    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "payments.csv"
        with csv_path.open("w", encoding="utf-8") as f:
            for pid in range(1, args.rows + 1):
                paid = start + timedelta(seconds=rng.randrange(0, 86400 * 365))
                f.write(
                    f"{pid},{rng.randrange(1, args.rows // 8 + 2)},"
                    f"{rng.randrange(5_000, 500_000) / 100:.2f},{rng.choice(statuses)},"
                    f"{paid:%Y-%m-%d %H:%M:%S}\n"
                )

        with psycopg.connect(PG_URL, autocommit=True) as pg:
            pg.execute("DROP SCHEMA IF EXISTS servicing CASCADE")
            pg.execute("CREATE SCHEMA servicing")
            pg.execute(f"CREATE TABLE servicing.payments ({DDL}, PRIMARY KEY (payment_id))")
            with pg.cursor().copy("COPY servicing.payments FROM STDIN WITH (FORMAT csv)") as cp:
                cp.write(csv_path.read_bytes())
            pg.execute("ANALYZE servicing.payments")

        db = HERE / "replica.duckdb"
        db.unlink(missing_ok=True)
        with duckdb.connect(str(db)) as dk:
            dk.execute(f"CREATE TABLE payments ({DDL.replace('NUMERIC', 'DECIMAL')})")
            dk.execute(f"INSERT INTO payments SELECT * FROM read_csv('{csv_path}', header=false)")

            ids = rng.sample(range(1, args.rows + 1), 9)
            deleted, amount_changed, status_changed = ids[:3], ids[3:6], ids[6:]
            extra = [args.rows + 1, args.rows + 2]
            dk.execute(f"DELETE FROM payments WHERE payment_id IN ({', '.join(map(str, deleted))})")
            for pid in extra:
                dk.execute(
                    "INSERT INTO payments VALUES (?, 1, 100.00, 'SETTLED', '2025-12-31 23:00:00')",
                    [pid],
                )
            for pid in amount_changed:
                dk.execute("UPDATE payments SET amount = amount + 0.01 WHERE payment_id = ?", [pid])
            for pid in status_changed:
                dk.execute(
                    "UPDATE payments SET status = CASE WHEN status = 'SETTLED' THEN 'RETURNED' "
                    "ELSE 'SETTLED' END WHERE payment_id = ?",
                    [pid],
                )

    print(f"seeded {args.rows:,} payments in Postgres servicing.payments and {db.name}")
    print("planted in the copy:")
    print(f"  deleted (missing in B):        {sorted(deleted)}")
    print(f"  added (missing in A):          {extra}")
    print(f"  amount changed by one cent:    {sorted(amount_changed)}")
    print(f"  status changed:                {sorted(status_changed)}")


if __name__ == "__main__":
    main()
