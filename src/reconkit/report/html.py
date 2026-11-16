"""Self-contained HTML report: one file, inline CSS, no scripts, no external requests."""

from __future__ import annotations

from html import escape

from reconkit.contracts.check import ContractResult
from reconkit.diff.model import DiffResult
from reconkit.runner import CheckOutcome, RunReport

_CSS = """
:root { --fg:#1c2128; --muted:#59636e; --line:#d1d9e0; --bg:#fff; --soft:#f6f8fa;
  --ok:#1a7f37; --bad:#cf222e; --warn:#9a6700; }
@media (prefers-color-scheme: dark) { :root { --fg:#e6edf3; --muted:#9198a1; --line:#3d444d;
  --bg:#0d1117; --soft:#151b23; --ok:#3fb950; --bad:#f85149; --warn:#d29922; } }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: var(--fg);
  background: var(--bg); margin: 0; padding: 24px 16px; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 0; display: inline; }
.meta, .muted { color: var(--muted); }
section { border: 1px solid var(--line); border-radius: 6px; margin: 16px 0; padding: 12px 16px; }
.badge { display: inline-block; font-weight: 600; font-size: 12px; padding: 1px 8px;
  border-radius: 10px; color: #fff; margin-right: 8px; text-transform: uppercase; }
.pass { background: var(--ok); } .fail, .error { background: var(--bad); }
.warn { background: var(--warn); } .skip { background: var(--muted); }
table { border-collapse: collapse; margin: 10px 0; width: 100%; }
th, td { border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top;
  font-variant-numeric: tabular-nums; }
th { background: var(--soft); font-weight: 600; }
td.num { text-align: right; } .scroll { overflow-x: auto; }
code, td.val { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.null { color: var(--muted); font-style: italic; }
details { margin-top: 8px; } summary { cursor: pointer; color: var(--muted); }
pre { background: var(--soft); padding: 8px; overflow-x: auto; white-space: pre-wrap; }
"""


def _val(value: object) -> str:
    if value is None:
        return '<td class="val"><span class="null">NULL</span></td>'
    return f'<td class="val">{escape(str(value))}</td>'


def _n(value: int | None) -> str:
    return "unknown" if value is None else f"{value:,}"


def _diff_html(result: DiffResult, max_rows: int) -> str:
    out = [
        '<div class="scroll"><table><tr><th>side</th><th>connection</th><th>table</th>'
        "<th>rows</th><th>queries</th><th>rows downloaded</th></tr>"
    ]
    for label, side in (("left", result.left), ("right", result.right)):
        out.append(
            f"<tr><td>{label}</td><td>{escape(side.connection)}</td>"
            f"<td><code>{escape(side.table)}</code></td><td class='num'>{_n(side.row_count)}</td>"
            f"<td class='num'>{side.queries}</td><td class='num'>{side.rows_fetched:,}</td></tr>"
        )
    out.append("</table></div>")
    notes = [
        f"strategy {escape(result.strategy)}",
        f"{result.levels} level(s)",
        f"key ({escape(', '.join(result.key_columns))})",
        f"{len(result.compared_columns)} columns compared",
    ]
    if result.sample_rate:
        notes.append(f"sample rate {result.sample_rate:g}")
    if result.within_tolerance:
        notes.append(f"{result.within_tolerance:,} rows differ only within tolerance")
    if result.truncated:
        notes.append("stopped at the configured limit")
    out.append(f'<p class="muted">{"; ".join(notes)}</p>')
    for warning in result.warnings:
        out.append(f'<p><span class="badge warn">warning</span>{escape(warning)}</p>')
    if result.is_match:
        return "".join(out)
    out.append("<table><tr><th>difference</th><th>rows</th></tr>")
    for label, count in (
        ("missing in left", result.missing_in_left),
        ("missing in right", result.missing_in_right),
        ("changed", result.changed),
        ("duplicate keys", len(result.duplicate_keys)),
    ):
        out.append(f"<tr><td>{label}</td><td class='num'>{count:,}</td></tr>")
    for column, count in sorted(result.column_change_counts().items(), key=lambda kv: -kv[1]):
        out.append(
            f"<tr><td>&nbsp;&nbsp;changed: <code>{escape(column)}</code></td>"
            f"<td class='num'>{count:,}</td></tr>"
        )
    out.append("</table>")
    shown = result.diffs[:max_rows]
    out.append(
        f"<details open><summary>{len(shown):,} of {len(result.diffs):,} differing rows"
        "</summary><div class='scroll'><table><tr><th>kind</th>"
    )
    out.extend(f"<th>{escape(k)}</th>" for k in result.key_columns)
    out.append("<th>column</th><th>left</th><th>right</th></tr>")
    for diff in shown:
        keys = "".join(_val(k) for k in diff.key)
        if diff.kind == "changed":
            for col in diff.columns:
                out.append(
                    f"<tr><td>changed</td>{keys}<td><code>{escape(col.column)}</code></td>"
                    f"{_val(col.left)}{_val(col.right)}</tr>"
                )
        else:
            out.append(
                f"<tr><td>{diff.kind.replace('_', ' ')}</td>{keys}<td></td><td></td><td></td></tr>"
            )
    out.append("</table></div></details>")
    return "".join(out)


def _contract_html(result: ContractResult) -> str:
    out = [
        f'<p class="muted">contract {escape(result.name)} version {escape(result.version)}</p>',
        "<div class='scroll'><table><tr><th>status</th><th>target</th><th>rule</th>"
        "<th>observed</th><th>expected</th><th>note</th></tr>",
    ]
    order = {"fail": 0, "error": 1, "warn": 2, "skip": 3, "pass": 4}
    for r in sorted(result.results, key=lambda r: order[r.status]):
        out.append(
            f'<tr><td><span class="badge {r.status}">{r.status}</span></td>'
            f"<td><code>{escape(r.target)}</code></td><td>{escape(r.rule)}</td>"
            f"<td>{escape(r.observed)}</td><td>{escape(r.expected)}</td>"
            f"<td>{escape(r.message)}</td></tr>"
        )
    out.append("</table></div>")
    return "".join(out)


def _outcome_html(outcome: CheckOutcome, max_rows: int) -> str:
    out = [
        f'<section><span class="badge {outcome.status}">{outcome.status}</span>'
        f"<h2>{escape(outcome.name)}</h2> <span class='muted'>{outcome.kind}, "
        f"{outcome.duration_seconds:.2f}s</span><p>{escape(outcome.summary)}</p>"
    ]
    if outcome.description:
        out.append(f'<p class="muted">{escape(outcome.description)}</p>')
    if outcome.diff is not None:
        out.append(_diff_html(outcome.diff, max_rows))
    if outcome.contract is not None:
        out.append(_contract_html(outcome.contract))
    if outcome.error:
        out.append(f"<pre>{escape(outcome.error)}</pre>")
    if outcome.audit_rows is not None:
        out.append(
            f'<p class="muted">{outcome.audit_rows:,} diff rows written to the audit table</p>'
        )
    out.append("</section>")
    return "".join(out)


def to_html(report: RunReport, max_rows: int = 200) -> str:
    body = "".join(_outcome_html(o, max_rows) for o in report.outcomes)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>reconkit report</title><style>{_CSS}</style></head><body><main>"
        "<h1>reconkit report</h1>"
        f'<p class="meta">run {escape(report.run_id)} started '
        f"{escape(report.started_at.strftime('%Y-%m-%d %H:%M:%S UTC'))}; "
        f"{len(report.outcomes)} check(s): {report.count('pass')} passed, "
        f"{report.count('fail')} failed, {report.count('error')} errored; "
        f"{report.duration_seconds:.2f}s</p>{body}</main></body></html>\n"
    )
