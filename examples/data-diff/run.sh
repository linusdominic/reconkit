#!/usr/bin/env bash
# Seed a Postgres table and a DuckDB copy with planted differences, then compare them
# with data-diff's command line, three ways. Run from the repository root or anywhere:
#   ./examples/data-diff/run.sh [--rows 1000000]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

export RECONKIT_PG_PASSWORD="${RECONKIT_PG_PASSWORD:-reconkit}"  # the local container's password
PG="postgresql://reconkit:${RECONKIT_PG_PASSWORD}@localhost:55439/reconkit"
REPLICA="duckdb://examples/data-diff/replica.duckdb"

docker compose up -d --wait postgres
uv run python examples/data-diff/seed.py "$@"

echo
echo "== the rows that differ (key, amount, status) =="
time uv run reconkit data-diff "$PG" servicing.payments "$REPLICA" payments \
    -k payment_id -c amount -c status

echo
echo "== --stats =="
uv run reconkit data-diff "$PG" servicing.payments "$REPLICA" payments \
    -k payment_id -c amount -c status --stats

echo
echo "== the same comparison from a data-diff TOML file =="
uv run reconkit data-diff --conf examples/data-diff/datadiff.toml --run payments --json

echo
echo "== as a CI gate: exit 1 when anything differs =="
set +e
uv run reconkit data-diff "$PG" servicing.payments "$REPLICA" payments -k payment_id \
    --fail-on-diff >/dev/null
echo "exit code: $?"
