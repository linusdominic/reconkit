"""Run the checks defined in a config file and collect their outcomes."""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from reconkit.adapters import Adapter, create_adapter
from reconkit.audit import write_audit
from reconkit.config import CheckConfig, Config, load_config
from reconkit.contracts.check import ContractResult, check_contract
from reconkit.contracts.model import load_contract
from reconkit.diff.engine import TableSpec, diff_tables
from reconkit.diff.model import DiffResult
from reconkit.errors import ConfigError

EXIT_OK = 0
EXIT_DIFFERENCES = 1
EXIT_CONFIG = 2
EXIT_ERROR = 3


@dataclass
class CheckOutcome:
    name: str
    kind: Literal["reconcile", "contract"]
    status: Literal["pass", "fail", "error"]
    summary: str
    duration_seconds: float = 0.0
    description: str | None = None
    diff: DiffResult | None = None
    contract: ContractResult | None = None
    error: str | None = None
    audit_rows: int | None = None

    def to_dict(self, max_rows: int | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
            "duration_seconds": round(self.duration_seconds, 4),
        }
        if self.description:
            out["description"] = self.description
        if self.diff is not None:
            out["diff"] = self.diff.to_dict(max_rows)
        if self.contract is not None:
            out["contract"] = self.contract.to_dict()
        if self.error is not None:
            out["error"] = self.error
        if self.audit_rows is not None:
            out["audit_rows_written"] = self.audit_rows
        return out


@dataclass
class RunReport:
    run_id: str
    started_at: datetime
    outcomes: list[CheckOutcome] = field(default_factory=list)
    duration_seconds: float = 0.0

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def exit_code(self) -> int:
        if self.count("error"):
            return EXIT_ERROR
        return EXIT_DIFFERENCES if self.count("fail") else EXIT_OK

    def to_dict(self, max_rows: int | None = None) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 4),
            "exit_code": self.exit_code,
            "summary": {s: self.count(s) for s in ("pass", "fail", "error")},
            "checks": [o.to_dict(max_rows) for o in self.outcomes],
        }


def summarize_diff(result: DiffResult) -> str:
    if result.is_match:
        rows = result.left.row_count
        scope = f"{rows:,} rows" if rows is not None else "all rows"
        return f"match ({scope}{', sampled' if result.sample_rate else ''})"
    parts = [
        f"{result.missing_in_left:,} missing in left",
        f"{result.missing_in_right:,} missing in right",
        f"{result.changed:,} changed",
    ]
    if result.duplicate_keys:
        parts.append(f"{len(result.duplicate_keys):,} duplicate keys")
    if result.truncated:
        parts.append("stopped at limit")
    return ", ".join(parts)


class Runner:
    """Owns the connections for one run. Use as a context manager."""

    def __init__(self, config: Config, adapters: dict[str, Adapter] | None = None) -> None:
        self.config = config
        self._adapters: dict[str, Adapter] = dict(adapters or {})
        self._owned: set[str] = set()

    def __enter__(self) -> Runner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for name in self._owned:
            self._adapters[name].close()
        self._owned.clear()

    def adapter(self, name: str) -> Adapter:
        if name not in self._adapters:
            settings = dict(self.config.connection(name))
            kind = str(settings.pop("type"))
            if kind == "duckdb" and "path" in settings and settings["path"] != ":memory:":
                settings["path"] = str(self.config.base_dir / str(settings["path"]))
            self._adapters[name] = create_adapter(kind, settings, name)
            self._owned.add(name)
        return self._adapters[name]

    def diff(self, check: CheckConfig) -> DiffResult:
        return diff_tables(
            TableSpec(
                self.adapter(check.left.connection),
                check.left.table,
                check.left.where,
                check.left.naive_timezone,
            ),
            TableSpec(
                self.adapter(check.right.connection),
                check.right.table,
                check.right.where,
                check.right.naive_timezone,
            ),
            check.key,
            columns=check.columns,
            ignore=check.ignore,
            mapping=check.mapping,
            tolerances={k: v.to_tolerance() for k, v in check.tolerances.items()},
            precision=check.precision,
            options=self.config.options_for(check),
        )

    def run_check(
        self, name: str, run_id: str, started_at: datetime, audit: bool = True
    ) -> CheckOutcome:
        check = self.config.checks[name]
        started = time.perf_counter()
        try:
            result = self.diff(check)
            outcome = CheckOutcome(
                name,
                "reconcile",
                "pass" if result.is_match else "fail",
                summarize_diff(result),
                description=check.description,
                diff=result,
            )
            target = check.audit or self.config.audit
            if audit and target is not None:
                outcome.audit_rows = write_audit(
                    self.adapter(target.connection),
                    target.table,
                    run_id=run_id,
                    check_name=name,
                    result=result,
                    recorded_at=started_at,
                    max_rows=target.max_rows,
                )
        except Exception as exc:
            outcome = CheckOutcome(
                name,
                "reconcile",
                "error",
                f"error: {_first_line(exc)}",
                description=check.description,
                error=str(exc),
            )
        outcome.duration_seconds = time.perf_counter() - started
        return outcome

    def run_contract(self, name: str) -> CheckOutcome:
        spec = self.config.contracts[name]
        started = time.perf_counter()
        try:
            contract = load_contract(self.config.base_dir / spec.contract)
            result = check_contract(self.adapter(spec.connection), contract, table=spec.table)
            failed = result.count("fail") + result.count("error")
            summary = (
                f"{result.count('pass')} rules passed"
                + (f", {failed} failed" if failed else "")
                + (f", {result.count('warn')} warnings" if result.count("warn") else "")
            )
            outcome = CheckOutcome(
                name, "contract", "pass" if result.passed else "fail", summary, contract=result
            )
        except Exception as exc:
            outcome = CheckOutcome(
                name, "contract", "error", f"error: {_first_line(exc)}", error=str(exc)
            )
        outcome.duration_seconds = time.perf_counter() - started
        return outcome

    def run(self, names: Sequence[str] | None = None, *, audit: bool = True) -> RunReport:
        """Run the named checks and contracts, or all of them when ``names`` is None."""
        known = [*self.config.checks, *self.config.contracts]
        selected = list(names) if names is not None else known
        unknown = [n for n in selected if n not in known]
        if unknown:
            raise ConfigError(f"unknown check(s) {unknown}; defined: {known}")
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        report = RunReport(uuid.uuid4().hex, started_at)
        for name in selected:
            if name in self.config.checks:
                report.outcomes.append(self.run_check(name, report.run_id, started_at, audit))
            else:
                report.outcomes.append(self.run_contract(name))
        report.duration_seconds = time.perf_counter() - started
        return report


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text.splitlines()[0]


def run_config(
    path: str | Path, names: Sequence[str] | None = None, *, audit: bool = True
) -> RunReport:
    """Load a config file and run its checks. Convenience for orchestrator integrations."""
    with Runner(load_config(path)) as runner:
        return runner.run(names, audit=audit)
