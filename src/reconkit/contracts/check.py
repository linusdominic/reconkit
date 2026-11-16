"""Validate a live table against a contract."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from reconkit.adapters.base import Adapter
from reconkit.contracts.model import Contract, Property, QualityRule, SchemaObject
from reconkit.diff.compare import parse_timestamp
from reconkit.errors import AdapterError, ConfigError
from reconkit.types import Canon, ColumnInfo, Kind, TableRef

Status = Literal["pass", "fail", "warn", "skip", "error"]

#: Column kinds each ODCS logicalType accepts.
_LOGICAL = {
    "string": {Kind.TEXT, Kind.UUID},
    "integer": {Kind.INTEGER},
    "number": {Kind.DECIMAL, Kind.FLOAT, Kind.INTEGER},
    "boolean": {Kind.BOOLEAN},
    "date": {Kind.DATE, Kind.TIMESTAMP},
    "timestamp": {Kind.TIMESTAMP},
}
_UNIT_SECONDS = {
    "s": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}
_PLACEHOLDER = re.compile(r"\$?\{(object|table|tableName|property|column|columnName)\}")


@dataclass(frozen=True)
class RuleResult:
    object: str
    prop: str | None
    rule: str
    status: Status
    observed: str = ""
    expected: str = ""
    message: str = ""

    @property
    def target(self) -> str:
        return f"{self.object}.{self.prop}" if self.prop else self.object


@dataclass
class ContractResult:
    name: str
    version: str
    results: list[RuleResult] = field(default_factory=list)
    duration_seconds: float = 0.0

    def count(self, status: Status) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def passed(self) -> bool:
        return not any(r.status in ("fail", "error") for r in self.results)

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": self.name,
            "version": self.version,
            "passed": self.passed,
            "summary": {s: self.count(s) for s in ("pass", "fail", "warn", "skip", "error")},
            "duration_seconds": round(self.duration_seconds, 4),
            "results": [
                {
                    "target": r.target,
                    "rule": r.rule,
                    "status": r.status,
                    "observed": r.observed,
                    "expected": r.expected,
                    "message": r.message,
                }
                for r in self.results
            ],
        }


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.6g}"


class _ObjectCheck:
    """Checks one schema object. Metrics are batched into a single aggregate query."""

    def __init__(
        self, adapter: Adapter, obj: SchemaObject, table: str, now: datetime, out: list[RuleResult]
    ) -> None:
        self.adapter, self.d, self.obj, self.now, self.out = adapter, adapter.dialect, obj, now, out
        self.ref = TableRef.parse(table)
        self.table_sql = self.d.table(self.ref)
        self.columns: dict[str, ColumnInfo] = {}
        #: (property, rule label, rule, aggregate SQL, post-processing kind)
        self.metrics: list[tuple[str | None, str, QualityRule, str, str]] = []

    def add(
        self,
        prop: str | None,
        rule: str,
        status: Status,
        observed: str = "",
        expected: str = "",
        message: str = "",
    ) -> None:
        self.out.append(RuleResult(self.obj.name, prop, rule, status, observed, expected, message))

    def run(self) -> None:
        try:
            self.columns = {c.name.lower(): c for c in self.adapter.get_columns(self.ref)}
        except AdapterError as exc:
            self.add(None, "table exists", "fail", message=str(exc).splitlines()[0])
            return
        self.add(None, "table exists", "pass", observed=str(self.ref))
        present = [p for p in self.obj.properties if self._check_schema(p)]
        declared = {p.name.lower() for p in self.obj.properties}
        for extra in sorted(set(self.columns) - declared):
            self.add(
                extra,
                "declared in contract",
                "warn",
                message="column exists in the table but not in the contract",
            )

        for prop in present:
            implied = []
            if prop.required:
                implied.append(QualityRule(name="required", metric="nullValues", mustBe=0))
            if prop.unique or (prop.primaryKey and self._single_pk()):
                implied.append(QualityRule(name="unique", metric="duplicateValues", mustBe=0))
            for rule in [*implied, *prop.quality]:
                self._plan_rule(prop.name, rule)
        for rule in self.obj.quality:
            self._plan_rule(None, rule)
        self._run_metrics()
        self._check_composite_key(present)
        for prop in present:
            self._check_relationships(prop)

    # -- schema -------------------------------------------------------------------------

    def _single_pk(self) -> bool:
        return sum(1 for p in self.obj.properties if p.primaryKey) == 1

    def _check_schema(self, prop: Property) -> bool:
        col = self.columns.get(prop.name.lower())
        if col is None:
            self.add(prop.name, "column exists", "fail", message="column not found in table")
            return False
        self.add(prop.name, "column exists", "pass")
        if prop.logicalType:
            allowed = _LOGICAL.get(prop.logicalType.lower())
            if allowed is None:
                self.add(
                    prop.name,
                    "logicalType",
                    "skip",
                    expected=prop.logicalType,
                    message="logicalType is not checked against the database",
                )
            else:
                ok = col.type.kind in allowed
                self.add(
                    prop.name,
                    "logicalType",
                    "pass" if ok else "fail",
                    observed=col.type.native,
                    expected=prop.logicalType,
                )
        if prop.physicalType:
            want = self.d.parse_type(prop.physicalType, None, None)
            ok = want.kind is col.type.kind and want.tz_aware == col.type.tz_aware
            if ok and want.kind is Kind.DECIMAL and None not in (want.scale, col.type.scale):
                ok = want.scale == col.type.scale
            observed = col.type.native
            if col.type.kind is Kind.DECIMAL and col.type.scale is not None:
                observed = f"{observed} (scale {col.type.scale})"
            self.add(
                prop.name,
                "physicalType",
                "pass" if ok else "fail",
                observed=observed,
                expected=prop.physicalType,
            )
        return True

    # -- metrics ------------------------------------------------------------------------

    def _plan_rule(self, prop: str | None, rule: QualityRule) -> None:
        label = rule.name or rule.metric or rule.type
        if rule.type in ("text", "custom"):
            self.add(prop, label, "skip", message=f"{rule.type} rules are not machine-checked")
            return
        if rule.type == "sql":
            self._run_sql_rule(prop, label, rule)
            return
        metric = rule.metric or ""
        if metric == "rowCount":
            self.metrics.append((prop, label, rule, "COUNT(*)", "count"))
            return
        if prop is None:
            self.add(prop, label, "error", message=f"{metric} must be declared on a property")
            return
        col = self.d.quote(self.columns[prop.lower()].name)
        text = self.d.cast_text(col)
        if metric == "nullValues":
            sql = f"SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END)"
        elif metric == "duplicateValues":
            sql = f"COUNT({col}) - COUNT(DISTINCT {col})"
        elif metric == "missingValues":
            missing = rule.arguments.get("missingValues", [""])
            members = ", ".join(self.d.literal(str(v)) for v in missing if v is not None)
            cond = f"{col} IS NULL" + (f" OR {text} IN ({members})" if members else "")
            sql = f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)"
        elif metric == "invalidValues":
            valid = rule.arguments.get("validValues")
            if not valid:
                self.add(prop, label, "error", message="invalidValues needs arguments.validValues")
                return
            members = ", ".join(self.d.literal(str(v)) for v in valid)
            sql = (
                f"SUM(CASE WHEN {col} IS NOT NULL AND {text} NOT IN ({members}) THEN 1 ELSE 0 END)"
            )
        else:  # freshness
            info = self.columns[prop.lower()]
            if info.type.kind not in (Kind.TIMESTAMP, Kind.DATE):
                self.add(prop, label, "error", message="freshness needs a date or timestamp")
                return
            sql = self.d.normalize(f"MAX({col})", info.type, Canon(Kind.TIMESTAMP))
            self.metrics.append((prop, label, rule, sql, "freshness"))
            return
        self.metrics.append((prop, label, rule, sql, "count"))

    def _run_metrics(self) -> None:
        if not self.metrics:
            return
        select = ", ".join(f"{sql} AS m{i}" for i, (_, _, _, sql, _) in enumerate(self.metrics))
        try:
            row = self.adapter.query(f"SELECT COUNT(*) AS total, {select} FROM {self.table_sql}")[0]
        except AdapterError as exc:
            for prop, label, *_ in self.metrics:
                self.add(prop, label, "error", message=str(exc).splitlines()[0])
            return
        total = int(row[0])
        for (prop, label, rule, _, kind), raw in zip(self.metrics, row[1:], strict=True):
            unit = (rule.unit or "").lower()
            if kind == "freshness":
                if raw is None:
                    self.add(prop, label, "fail", observed="no rows", message="table is empty")
                    continue
                seconds = (self.now - parse_timestamp(str(raw))).total_seconds()
                divisor = _UNIT_SECONDS.get(unit or "hours")
                if divisor is None:
                    self.add(prop, label, "error", message=f"unknown freshness unit {rule.unit!r}")
                    continue
                value = seconds / divisor
                observed = f"{value:.2f} {unit or 'hours'} old (latest {raw} UTC)"
            else:
                value = float(raw or 0)
                observed = _fmt(value)
                if unit in ("percent", "%"):
                    value = 100.0 * value / total if total else 0.0
                    observed = f"{value:.4g}% ({_fmt(float(raw or 0))} of {total})"
            self._judge(prop, label, rule, value, observed)

    def _judge(
        self, prop: str | None, label: str, rule: QualityRule, value: float, observed: str
    ) -> None:
        if not rule.operators():
            self.add(
                prop,
                label,
                "error",
                observed=observed,
                message="rule has no operator (mustBe, mustBeLessThan, ...)",
            )
            return
        passed, expected = rule.evaluate(value)
        self.add(prop, label, "pass" if passed else "fail", observed=observed, expected=expected)

    def _run_sql_rule(self, prop: str | None, label: str, rule: QualityRule) -> None:
        def fill(match: re.Match[str]) -> str:
            if match.group(1) in ("object", "table", "tableName"):
                return self.table_sql
            if prop is None:
                raise ConfigError(f"rule {label!r}: property placeholder used at object level")
            return self.d.quote(self.columns[prop.lower()].name)

        try:
            rows = self.adapter.query(_PLACEHOLDER.sub(fill, rule.query or ""))
            value = float(rows[0][0] or 0) if rows else 0.0
        except (AdapterError, ConfigError, TypeError, ValueError) as exc:
            self.add(prop, label, "error", message=str(exc).splitlines()[0])
            return
        self._judge(prop, label, rule, value, _fmt(value))

    def _check_composite_key(self, present: list[Property]) -> None:
        pk = [p for p in present if p.primaryKey]
        if len(pk) < 2:
            return
        cols = ", ".join(self.d.quote(self.columns[p.name.lower()].name) for p in pk)
        sql = (
            f"SELECT COUNT(*) FROM (SELECT {cols} FROM {self.table_sql} "
            f"GROUP BY {cols} HAVING COUNT(*) > 1) d"
        )
        label = f"primary key ({', '.join(p.name for p in pk)}) unique"
        try:
            dupes = int(self.adapter.query(sql)[0][0])
        except AdapterError as exc:
            self.add(None, label, "error", message=str(exc).splitlines()[0])
            return
        self.add(
            None,
            label,
            "pass" if dupes == 0 else "fail",
            observed=f"{dupes} duplicated",
            expected="mustBe 0",
        )

    def _check_relationships(self, prop: Property) -> None:
        col = self.d.quote(self.columns[prop.name.lower()].name)
        for rel in prop.relationships:
            label = f"references {rel.to}"
            try:
                table, column = rel.target()
                parent = self.d.table(TableRef.parse(table))
                sql = (
                    f"SELECT COUNT(*) FROM {self.table_sql} c WHERE c.{col} IS NOT NULL AND "
                    f"NOT EXISTS (SELECT 1 FROM {parent} p "
                    f"WHERE p.{self.d.quote(column)} = c.{col})"
                )
                orphans = int(self.adapter.query(sql)[0][0])
            except (AdapterError, ConfigError) as exc:
                self.add(prop.name, label, "error", message=str(exc).splitlines()[0])
                continue
            self.add(
                prop.name,
                label,
                "pass" if orphans == 0 else "fail",
                observed=f"{orphans} orphaned",
                expected="mustBe 0",
            )


def check_contract(
    adapter: Adapter,
    contract: Contract,
    *,
    table: str | None = None,
    now: datetime | None = None,
) -> ContractResult:
    """Check every schema object of ``contract`` against the database behind ``adapter``.

    ``table`` overrides the physical table name and is only valid for a contract with one
    schema object. ``now`` (UTC) is injectable so freshness checks are testable.
    """
    if table is not None and len(contract.objects) != 1:
        raise ConfigError("a table override needs a contract with exactly one schema object")
    started = time.perf_counter()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    result = ContractResult(contract.name or contract.id or "contract", contract.version)
    for obj in contract.objects:
        _ObjectCheck(adapter, obj, table or obj.table, current, result.results).run()
    result.duration_seconds = time.perf_counter() - started
    return result
