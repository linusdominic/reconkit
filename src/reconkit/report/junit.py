"""JUnit XML report, so CI systems list each check as a test.

A reconciliation check is one test case. A contract check is one test case per rule,
which lets CI show exactly which guarantee failed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from reconkit.diff.model import DiffResult
from reconkit.runner import CheckOutcome, RunReport


def _diff_detail(result: DiffResult, max_rows: int) -> str:
    lines = [
        f"left:  {result.left.table} ({result.left.row_count} rows)",
        f"right: {result.right.table} ({result.right.row_count} rows)",
        f"missing in left: {result.missing_in_left}",
        f"missing in right: {result.missing_in_right}",
        f"changed: {result.changed}",
    ]
    lines += [f"  changed column {c}: {n} rows" for c, n in result.column_change_counts().items()]
    for diff in result.diffs[:max_rows]:
        key = ", ".join(f"{k}={v}" for k, v in zip(result.key_columns, diff.key, strict=True))
        if diff.kind == "changed":
            cols = "; ".join(f"{c.column}: {c.left!r} -> {c.right!r}" for c in diff.columns)
            lines.append(f"changed [{key}] {cols}")
        else:
            lines.append(f"{diff.kind} [{key}]")
    if len(result.diffs) > max_rows:
        lines.append(f"... {len(result.diffs) - max_rows} more")
    return "\n".join(lines)


def _add_cases(suite: ET.Element, outcome: CheckOutcome, max_rows: int) -> None:
    if outcome.contract is not None:
        for rule in outcome.contract.results:
            case = ET.SubElement(
                suite,
                "testcase",
                classname=f"reconkit.contract.{outcome.name}",
                name=f"{rule.target}: {rule.rule}",
                time="0",
            )
            detail = f"observed: {rule.observed}\nexpected: {rule.expected}\n{rule.message}"
            if rule.status == "fail":
                ET.SubElement(case, "failure", message=f"{rule.target}: {rule.rule}").text = detail
            elif rule.status == "error":
                ET.SubElement(case, "error", message=rule.message or rule.rule).text = detail
            elif rule.status == "skip":
                ET.SubElement(case, "skipped", message=rule.message)
        return
    case = ET.SubElement(
        suite,
        "testcase",
        classname=f"reconkit.{outcome.kind}",
        name=outcome.name,
        time=f"{outcome.duration_seconds:.3f}",
    )
    if outcome.status == "error":
        ET.SubElement(case, "error", message=outcome.summary).text = outcome.error or ""
    elif outcome.status == "fail":
        failure = ET.SubElement(case, "failure", message=outcome.summary)
        if outcome.diff is not None:
            failure.text = _diff_detail(outcome.diff, max_rows)


def to_junit(report: RunReport, max_rows: int = 50) -> str:
    suite = ET.Element(
        "testsuite",
        name="reconkit",
        timestamp=report.started_at.isoformat(),
        time=f"{report.duration_seconds:.3f}",
    )
    for outcome in report.outcomes:
        _add_cases(suite, outcome, max_rows)
    suite.set("tests", str(len(suite.findall("testcase"))))
    suite.set("failures", str(len(suite.findall("testcase/failure"))))
    suite.set("errors", str(len(suite.findall("testcase/error"))))
    suite.set("skipped", str(len(suite.findall("testcase/skipped"))))
    root = ET.Element("testsuites")
    root.append(suite)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
