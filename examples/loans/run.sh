#!/usr/bin/env bash
# One command: start Postgres, seed both systems, reconcile, check the contract.
# Run from anywhere:  ./examples/loans/run.sh
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"

export RECONKIT_PG_PASSWORD="${RECONKIT_PG_PASSWORD:-reconkit}"  # the local container's password

docker compose -f "$ROOT/docker-compose.yml" up -d --wait
uv run --project "$ROOT" python seed.py "$@"

echo
echo "== reconkit run --all =="
set +e
uv run --project "$ROOT" reconkit run -c reconkit.yml --all \
    --json report.json --junit report.xml --html report.html
code=$?
set -e
echo "exit code: $code (1 means differences were found, which is what the seed planted)"

echo
echo "== reconkit contract diff (current vs. proposed) =="
uv run --project "$ROOT" reconkit contract diff contracts/fct_loans.yml \
    contracts/fct_loans_proposed.yml || true

echo
echo "reports written: $(pwd)/report.html, report.json, report.xml"
