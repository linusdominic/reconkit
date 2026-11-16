"""Terminal report (rich)."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from reconkit.contracts.check import ContractResult
from reconkit.diff.model import DiffResult
from reconkit.runner import RunReport

_STYLE = {"pass": "green", "fail": "red", "error": "red", "warn": "yellow", "skip": "dim"}


def _cell(value: object) -> str:
    return "[dim]NULL[/dim]" if value is None else escape(str(value))


def _rows(n: int | None) -> str:
    return "?" if n is None else f"{n:,}"


def render_diff(console: Console, result: DiffResult, max_rows: int = 20) -> None:
    left, right = result.left, result.right
    console.print(
        f"  left   {escape(left.connection)}:{escape(left.table)}  {_rows(left.row_count)} rows  "
        f"[dim]{left.queries} queries, {left.rows_fetched:,} rows downloaded[/dim]"
    )
    console.print(
        f"  right  {escape(right.connection)}:{escape(right.table)}  "
        f"{_rows(right.row_count)} rows  "
        f"[dim]{right.queries} queries, {right.rows_fetched:,} rows downloaded[/dim]"
    )
    extras = [f"strategy {result.strategy}", f"{result.levels} level(s)"]
    if result.sample_rate:
        extras.append(f"sample {result.sample_rate:g}")
    if result.within_tolerance:
        extras.append(f"{result.within_tolerance:,} rows within tolerance")
    console.print(f"  [dim]{', '.join(extras)}[/dim]")
    for warning in result.warnings:
        console.print(f"  [yellow]warning:[/yellow] {escape(warning)}")
    if result.is_match:
        return

    summary = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    summary.add_column("difference")
    summary.add_column("rows", justify="right")
    summary.add_row("missing in left", f"{result.missing_in_left:,}")
    summary.add_row("missing in right", f"{result.missing_in_right:,}")
    summary.add_row("changed", f"{result.changed:,}")
    for column, count in sorted(result.column_change_counts().items(), key=lambda kv: -kv[1]):
        summary.add_row(f"  changed: {escape(column)}", f"{count:,}")
    if result.duplicate_keys:
        summary.add_row("duplicate keys", f"{len(result.duplicate_keys):,}")
    console.print(summary)

    if max_rows <= 0 or not result.diffs:
        return
    table = Table(show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("kind")
    table.add_column(", ".join(result.key_columns))
    table.add_column("column")
    table.add_column("left")
    table.add_column("right")
    for diff in result.diffs[:max_rows]:
        key = ", ".join(_cell(k) for k in diff.key)
        if diff.kind == "changed":
            for i, col in enumerate(diff.columns):
                table.add_row(
                    "changed" if i == 0 else "",
                    key if i == 0 else "",
                    escape(col.column),
                    _cell(col.left),
                    _cell(col.right),
                )
        else:
            table.add_row(diff.kind.replace("_", " "), key, "", "", "")
    console.print(table)
    if len(result.diffs) > max_rows:
        console.print(
            f"  [dim]... {len(result.diffs) - max_rows:,} more differing rows "
            "(see the JSON or HTML report)[/dim]"
        )


def render_contract(console: Console, result: ContractResult, verbose: bool = False) -> None:
    shown = [r for r in result.results if verbose or r.status != "pass"]
    if not shown:
        return
    table = Table(show_header=True, header_style="bold", padding=(0, 1))
    for heading in ("status", "target", "rule", "observed", "expected", "note"):
        table.add_column(heading)
    for r in shown:
        style = _STYLE[r.status]
        table.add_row(
            f"[{style}]{r.status}[/{style}]",
            escape(r.target),
            escape(r.rule),
            escape(r.observed),
            escape(r.expected),
            escape(r.message),
        )
    console.print(table)


def render_terminal(
    report: RunReport, console: Console | None = None, *, max_rows: int = 20, verbose: bool = False
) -> None:
    console = console or Console()
    for outcome in report.outcomes:
        style = _STYLE[outcome.status]
        console.print(
            f"[bold {style}]{outcome.status.upper():5}[/bold {style}] "
            f"[bold]{escape(outcome.name)}[/bold]  {escape(outcome.summary)}  "
            f"[dim]{outcome.duration_seconds:.2f}s[/dim]"
        )
        if outcome.diff is not None:
            render_diff(console, outcome.diff, max_rows)
            if outcome.audit_rows is not None:
                console.print(f"  [dim]audit: {outcome.audit_rows:,} diff rows written[/dim]")
        if outcome.contract is not None:
            render_contract(console, outcome.contract, verbose)
        if outcome.error and outcome.status == "error":
            console.print(f"  [red]{escape(outcome.error)}[/red]")
    total = len(report.outcomes)
    console.print(
        f"\n{total} check(s): {report.count('pass')} passed, {report.count('fail')} failed, "
        f"{report.count('error')} errored in {report.duration_seconds:.2f}s"
    )
