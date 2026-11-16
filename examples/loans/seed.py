"""Seed the loan-origination example.

Creates ``lending.loans`` in Postgres (the system of record) and a warehouse copy,
``analytics.fct_loans``, in a local DuckDB file. The warehouse copy is what a typical
nightly ETL would produce, including its habits:

* ``status`` is renamed to ``loan_status``
* ``interest_rate`` is a DOUBLE instead of NUMERIC(7,4)
* ``branch_code`` lost its CHAR(6) padding
* timestamps are timezone-naive and in Asia/Singapore local time, not UTC

and a known set of real discrepancies, printed at the end, so you can check that reconkit
reports exactly those.

Usage: python seed.py [--rows 50000]
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import psycopg

HERE = Path(__file__).parent
PG_URL = os.environ.get(
    "RECONKIT_EXAMPLE_PG_URL", "postgresql://reconkit:reconkit@localhost:55439/reconkit"
)
SGT = timedelta(hours=8)
COLUMNS = [
    "loan_id", "customer_id", "product_code", "status", "principal_amount", "interest_rate",
    "term_months", "currency", "originated_at", "disbursed_at", "is_secured", "branch_code",
]  # fmt: skip


def generate(rows: int, rng: random.Random) -> list[dict[str, object]]:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    out = []
    for loan_id in range(1, rows + 1):
        status = rng.choices(
            ["APPROVED", "DISBURSED", "REJECTED", "WITHDRAWN", "CLOSED"], [15, 55, 15, 5, 10]
        )[0]
        originated = start + timedelta(seconds=rng.randrange(0, 86400 * 600))
        disbursed = (
            originated + timedelta(hours=rng.randrange(2, 240))
            if status in ("DISBURSED", "CLOSED")
            else None
        )
        out.append(
            {
                "loan_id": loan_id,
                "customer_id": rng.randrange(1, rows // 4 + 1),
                "product_code": rng.choice(["PL-STD", "PL-PLUS", "HL-FIX", "HL-FLT", "AUTO"]),
                "status": status,
                "principal_amount": Decimal(rng.randrange(100_000, 75_000_000)) / 100,
                "interest_rate": Decimal(rng.randrange(18_000, 129_999)) / 10_000,
                "term_months": rng.choice([12, 24, 36, 48, 60, 120, 240, 360]),
                "currency": "SGD",
                "originated_at": originated,
                "disbursed_at": disbursed,
                "is_secured": rng.random() < 0.4,
                "branch_code": rng.choice(["SG01", "SG02", "SG114", "SGHQ"]),
            }
        )
    return out


def fmt(value: object, *, local: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if local:  # warehouse convention: naive local time
            return (value + SGT).strftime("%Y-%m-%d %H:%M:%S")
        return value.strftime("%Y-%m-%d %H:%M:%S+00")
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    source = generate(args.rows, rng)

    # ---- the warehouse copy and its discrepancies --------------------------------------
    warehouse = [dict(r) for r in source]
    by_id = {r["loan_id"]: r for r in warehouse}
    ids = rng.sample(range(1, args.rows + 1), 45)
    late, drifted, stale, shifted, rounded = ids[:7], ids[7:12], ids[12:16], ids[16:22], ids[22:]
    for loan_id in late:  # not yet loaded
        warehouse.remove(by_id[loan_id])
    for loan_id in drifted:  # amount corrected in the source after the load
        by_id[loan_id]["principal_amount"] += Decimal(rng.choice(["0.01", "-250.00", "1000.00"]))
    for loan_id in stale:  # status change never propagated
        row = by_id[loan_id]
        row["status"] = "APPROVED" if row["status"] != "APPROVED" else "DISBURSED"
    for loan_id in shifted:  # one batch was loaded as UTC instead of local time
        by_id[loan_id]["originated_at"] -= SGT
    for loan_id in rounded:  # rate rounded to 3 decimals by an old job; inside tolerance
        row = by_id[loan_id]
        row["interest_rate"] = row["interest_rate"].quantize(Decimal("0.001"))
    ghosts = []  # hard-deleted in the source, still in the warehouse
    for i in range(3):
        ghost = dict(source[i])
        ghost["loan_id"] = args.rows + 1000 + i
        ghosts.append(ghost)
    warehouse.extend(ghosts)
    unknown = next(i for i in range(11, args.rows) if i not in ids)
    by_id[unknown]["status"] = "UNKNOWN"  # a status the contract does not allow

    with tempfile.TemporaryDirectory() as tmp:
        src_csv, wh_csv = Path(tmp) / "source.csv", Path(tmp) / "warehouse.csv"
        for path, rows, local in ((src_csv, source, False), (wh_csv, warehouse, True)):
            with path.open("w", newline="") as handle:
                writer = csv.writer(handle)
                for r in rows:
                    writer.writerow([fmt(r[c], local=local) for c in COLUMNS])

        with psycopg.connect(PG_URL, autocommit=True) as pg:
            pg.execute("CREATE SCHEMA IF NOT EXISTS lending")
            pg.execute("DROP TABLE IF EXISTS lending.loans")
            pg.execute(
                """
                CREATE TABLE lending.loans (
                    loan_id          bigint PRIMARY KEY,
                    customer_id      bigint NOT NULL,
                    product_code     text NOT NULL,
                    status           text NOT NULL,
                    principal_amount numeric(14,2) NOT NULL,
                    interest_rate    numeric(7,4) NOT NULL,
                    term_months      integer NOT NULL,
                    currency         char(3) NOT NULL,
                    originated_at    timestamptz NOT NULL,
                    disbursed_at     timestamptz,
                    is_secured       boolean NOT NULL,
                    branch_code      char(6) NOT NULL
                )
                """
            )
            with (
                pg.cursor() as cur,
                cur.copy("COPY lending.loans FROM STDIN WITH (FORMAT csv, NULL '')") as copy,
                src_csv.open("rb") as handle,
            ):
                while chunk := handle.read(1 << 20):
                    copy.write(chunk)
            pg.execute("ANALYZE lending.loans")

        db_path = HERE / "warehouse.duckdb"
        db_path.unlink(missing_ok=True)
        with duckdb.connect(str(db_path)) as wh:
            wh.execute("CREATE SCHEMA analytics")
            wh.execute(
                f"""
                CREATE TABLE analytics.fct_loans AS
                SELECT *, CAST(now() AT TIME ZONE 'UTC' AS TIMESTAMP) AS _loaded_at
                FROM read_csv('{wh_csv}', header = false, nullstr = '', columns = {{
                    'loan_id': 'BIGINT', 'customer_id': 'BIGINT', 'product_code': 'VARCHAR',
                    'loan_status': 'VARCHAR', 'principal_amount': 'DECIMAL(14,2)',
                    'interest_rate': 'DOUBLE', 'term_months': 'INTEGER', 'currency': 'VARCHAR',
                    'originated_at': 'TIMESTAMP', 'disbursed_at': 'TIMESTAMP',
                    'is_secured': 'BOOLEAN', 'branch_code': 'VARCHAR'}})
                """
            )
            # Customer dimension for the contract's referential check; the customers of the
            # two most recent loans are missing from it.
            wh.execute(
                """
                CREATE TABLE analytics.dim_customers AS
                SELECT DISTINCT customer_id FROM analytics.fct_loans
                WHERE customer_id NOT IN (
                    SELECT customer_id FROM analytics.fct_loans ORDER BY loan_id DESC LIMIT 2)
                """
            )

    print(f"seeded {len(source):,} loans in Postgres and {len(warehouse):,} in DuckDB")
    print("discrepancies planted in the warehouse copy:")
    print(f"  missing in warehouse (late arriving):   {sorted(late)}")
    print(f"  only in warehouse (deleted in source):  {[g['loan_id'] for g in ghosts]}")
    print(f"  principal_amount drifted:               {sorted(drifted)}")
    print(
        f"  status not propagated:                  {sorted(stale)} + [{unknown}] (set to UNKNOWN)"
    )
    print(f"  originated_at shifted by 8 hours:       {sorted(shifted)}")
    print(f"  interest_rate rounded (within tolerance): {len(rounded)} rows")


if __name__ == "__main__":
    main()
