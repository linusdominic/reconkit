"""JSON report."""

from __future__ import annotations

import json

from reconkit.runner import RunReport


def to_json(report: RunReport, max_rows: int | None = None) -> str:
    return json.dumps(report.to_dict(max_rows), indent=2, default=str)
