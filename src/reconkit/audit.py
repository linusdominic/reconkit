"""Persist diff results to an audit table.

Two tables are written: ``<table>_runs`` gets one row per check run (so a clean run still
leaves evidence that it happened) and ``<table>`` gets one row per differing column value
or missing row. Both are append-only; reconkit never updates or deletes audit rows.
"""

from __future__ import annotations

import json
from datetime import datetime

from reconkit.adapters.base import Adapter
from reconkit.diff.model import DiffResult
from reconkit.types import TableRef

_RUN_COLUMNS = (
    "run_id",
    "check_name",
    "recorded_at",
    "status",
    "left_table",
    "right_table",
    "strategy",
    "left_rows",
    "right_rows",
    "missing_in_left",
    "missing_in_right",
    "changed",
    "duration_seconds",
)
_DIFF_COLUMNS = (
    "run_id",
    "check_name",
    "recorded_at",
    "diff_kind",
    "row_key",
    "column_name",
    "left_value",
    "right_value",
)


def _tables(adapter: Adapter, table: str) -> tuple[str, str]:
    ref = TableRef.parse(table)
    runs = TableRef((*ref.parts[:-1], f"{ref.name}_runs"))
    return adapter.dialect.table(ref), adapter.dialect.table(runs)


def ensure_tables(adapter: Adapter, table: str) -> None:
    text = adapter.dialect.text_type
    diffs, runs = _tables(adapter, table)
    adapter.execute(
        f"CREATE TABLE IF NOT EXISTS {runs} (run_id {text}, check_name {text}, "
        f"recorded_at TIMESTAMP, status {text}, left_table {text}, right_table {text}, "
        f"strategy {text}, left_rows BIGINT, right_rows BIGINT, missing_in_left BIGINT, "
        "missing_in_right BIGINT, changed BIGINT, duration_seconds DOUBLE PRECISION)"
    )
    adapter.execute(
        f"CREATE TABLE IF NOT EXISTS {diffs} (run_id {text}, check_name {text}, "
        f"recorded_at TIMESTAMP, diff_kind {text}, row_key {text}, column_name {text}, "
        f"left_value {text}, right_value {text})"
    )


def write_audit(
    adapter: Adapter,
    table: str,
    *,
    run_id: str,
    check_name: str,
    result: DiffResult,
    recorded_at: datetime,
    max_rows: int = 10_000,
) -> int:
    """Append one run row and up to ``max_rows`` diff rows. Returns diff rows written."""
    ensure_tables(adapter, table)
    diffs, runs = _tables(adapter, table)
    mark = adapter.dialect.placeholder
    when = recorded_at.replace(tzinfo=None)

    def insert(target: str, columns: tuple[str, ...]) -> str:
        return (
            f"INSERT INTO {target} ({', '.join(columns)}) "
            f"VALUES ({', '.join([mark] * len(columns))})"
        )

    adapter.executemany(
        insert(runs, _RUN_COLUMNS),
        [
            (
                run_id,
                check_name,
                when,
                "match" if result.is_match else "mismatch",
                result.left.table,
                result.right.table,
                result.strategy,
                result.left.row_count,
                result.right.row_count,
                result.missing_in_left,
                result.missing_in_right,
                result.changed,
                result.duration_seconds,
            )
        ],
    )
    rows: list[tuple[object, ...]] = []
    for diff in result.diffs:
        if len(rows) >= max_rows:
            break
        key = json.dumps(dict(zip(result.key_columns, diff.key, strict=True)), sort_keys=True)
        if diff.kind == "changed":
            for col in diff.columns:
                rows.append(
                    (run_id, check_name, when, diff.kind, key, col.column, col.left, col.right)
                )
        else:
            rows.append((run_id, check_name, when, diff.kind, key, None, None, None))
    adapter.executemany(insert(diffs, _DIFF_COLUMNS), rows[:max_rows])
    return min(len(rows), max_rows)
