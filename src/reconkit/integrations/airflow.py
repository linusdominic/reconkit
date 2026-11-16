"""Airflow operator.

Importable without Airflow installed (it then subclasses a minimal stand-in), so DAG
parsing environments and unit tests do not need the full dependency.

    from reconkit.integrations.airflow import ReconkitOperator

    reconcile = ReconkitOperator(
        task_id="reconcile_loans",
        config_path="/opt/airflow/dags/reconkit.yml",
        checks=["loans"],
    )
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from reconkit.report import to_json
from reconkit.runner import EXIT_OK, run_config

try:  # Airflow 3
    from airflow.sdk import BaseOperator
except ImportError:
    try:  # Airflow 2
        from airflow.models import BaseOperator
    except ImportError:

        class BaseOperator:  # type: ignore[no-redef]
            """Stand-in used when Airflow is not installed."""

            def __init__(self, *, task_id: str = "reconkit", **kwargs: Any) -> None:
                self.task_id = task_id


try:
    from airflow.exceptions import AirflowException
except ImportError:

    class AirflowException(Exception):  # type: ignore[no-redef]
        """Stand-in used when Airflow is not installed."""


class ReconkitOperator(BaseOperator):  # type: ignore[misc]
    """Run reconkit checks; fail the task when a check fails or cannot run.

    :param config_path: path to ``reconkit.yml`` on the worker.
    :param checks: check/contract names to run; all of them when omitted.
    :param fail_on_diff: when False, differences are reported (XCom, logs) without failing
        the task. Errors (connection failures and the like) always fail it.
    :param json_path: optional path to also write the full JSON report to.

    The return value (pushed to XCom) is a small summary dict, not the diff rows.
    """

    template_fields: Sequence[str] = ("config_path", "checks", "json_path")

    def __init__(
        self,
        *,
        config_path: str,
        checks: Sequence[str] | None = None,
        fail_on_diff: bool = True,
        audit: bool = True,
        json_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.config_path = config_path
        self.checks = checks
        self.fail_on_diff = fail_on_diff
        self.audit = audit
        self.json_path = json_path

    def execute(self, context: Any) -> dict[str, Any]:
        report = run_config(self.config_path, self.checks, audit=self.audit)
        if self.json_path:
            with open(self.json_path, "w", encoding="utf-8") as handle:
                handle.write(to_json(report))
        summary = {
            "run_id": report.run_id,
            "exit_code": report.exit_code,
            "checks": {o.name: {"status": o.status, "summary": o.summary} for o in report.outcomes},
        }
        errored = [o.name for o in report.outcomes if o.status == "error"]
        failed = [o.name for o in report.outcomes if o.status == "fail"]
        if errored:
            raise AirflowException(f"reconkit checks could not run: {errored}; {summary}")
        if failed and self.fail_on_diff and report.exit_code != EXIT_OK:
            raise AirflowException(f"reconkit checks failed: {failed}; {summary}")
        return summary
