# Contributing

## Setup

You need [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync                          # creates .venv with dev dependencies
docker compose up -d --wait      # Postgres 16 on localhost:55439, for integration tests
uv run pre-commit install        # optional: format, lint and type-check on commit
```

## Checks

All of these run in CI and must pass:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy                      # strict
uv run pytest --cov              # unit, property-based and Postgres integration tests
```

`tests/unit` needs no database (it uses in-memory DuckDB). `tests/integration` needs the
Postgres container; without it those tests are skipped locally. In CI
`RECONKIT_REQUIRE_PG=1` turns a missing database into a failure.

To point the tests at a different Postgres, set `RECONKIT_TEST_PG_URL`.

## Changing generated SQL

`tests/snapshots/*.sql` holds the exact SQL each dialect generates. If you change SQL
generation on purpose, regenerate and review the diff:

```bash
RECONKIT_UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/test_dialects.py
git diff tests/snapshots
```

For PostgreSQL and DuckDB the integration and property tests then tell you whether the two
engines still agree. For Snowflake, BigQuery and Redshift the snapshot and a sqlglot parse
are all there is, so read those diffs carefully, and if you have access to an account,
say in the pull request what you ran.

## Adding an adapter

See [docs/adapters.md](docs/adapters.md). The bar for calling an adapter "supported" is
an integration test that runs the cross-engine type matrix
(`tests/integration/test_postgres.py::test_type_matrix_is_identical_across_engines`)
against it.

## Pull requests

- One logical change per pull request, with tests.
- Add a line to `CHANGELOG.md` under an "Unreleased" heading.
- Design changes that alter a decision in `docs/adr/` should add a new ADR that
  supersedes the old one rather than editing history.
