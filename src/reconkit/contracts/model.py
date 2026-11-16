"""Contract file model.

The shape follows the Open Data Contract Standard (ODCS) v3: a ``schema`` list of objects
with ``properties`` and ``quality`` rules. reconkit reads the subset it can verify against
a database and ignores the rest (descriptions, team, pricing, ...), so an existing ODCS
file loads unchanged. The ``freshness`` metric is a reconkit extension, not part of ODCS.
See docs/contracts.md for the exact list of what is and is not supported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, model_validator

from reconkit.errors import ConfigError

Number = float

#: ODCS v3.0 rule names mapped to the v3.1 metric names used internally.
METRIC_ALIASES = {
    "duplicatecount": "duplicateValues",
    "duplicatevalues": "duplicateValues",
    "nullcount": "nullValues",
    "nullvalues": "nullValues",
    "missingvalues": "missingValues",
    "invalidvalues": "invalidValues",
    "rowcount": "rowCount",
    "freshness": "freshness",
}
OPERATORS = (
    "mustBe",
    "mustNotBe",
    "mustBeGreaterThan",
    "mustBeGreaterOrEqualTo",
    "mustBeLessThan",
    "mustBeLessOrEqualTo",
    "mustBeBetween",
    "mustNotBeBetween",
)


def _holds(op: str, value: float, bound: Any) -> bool:
    if op == "mustBe":
        return bool(value == bound)
    if op == "mustNotBe":
        return bool(value != bound)
    if op == "mustBeGreaterThan":
        return bool(value > bound)
    if op == "mustBeGreaterOrEqualTo":
        return bool(value >= bound)
    if op == "mustBeLessThan":
        return bool(value < bound)
    if op == "mustBeLessOrEqualTo":
        return bool(value <= bound)
    inside = bool(bound[0] <= value <= bound[1])
    return inside if op == "mustBeBetween" else not inside


def _number(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_number(v) for v in value) + "]"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def describe_operators(ops: dict[str, Any]) -> str:
    return ", ".join(f"{op} {_number(bound)}" for op, bound in ops.items()) or "(none)"


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class QualityRule(_Loose):
    name: str | None = None
    description: str | None = None
    type: Literal["library", "sql", "text", "custom"] = "library"
    metric: str | None = Field(default=None, validation_alias=AliasChoices("metric", "rule"))
    query: str | None = None
    arguments: dict[str, Any] = {}
    unit: str | None = None
    mustBe: Number | None = None
    mustNotBe: Number | None = None
    mustBeGreaterThan: Number | None = None
    mustBeGreaterOrEqualTo: Number | None = None
    mustBeLessThan: Number | None = None
    mustBeLessOrEqualTo: Number | None = None
    mustBeBetween: list[Number] | None = None
    mustNotBeBetween: list[Number] | None = None

    @model_validator(mode="after")
    def _check(self) -> QualityRule:
        if self.type == "library":
            if not self.metric:
                raise ValueError("library rule needs a 'metric'")
            canonical = METRIC_ALIASES.get(self.metric.lower())
            if canonical is None:
                raise ValueError(
                    f"unsupported metric {self.metric!r} "
                    f"(supported: {sorted(set(METRIC_ALIASES.values()))})"
                )
            self.metric = canonical
        if self.type == "sql" and not self.query:
            raise ValueError("sql rule needs a 'query'")
        for op in ("mustBeBetween", "mustNotBeBetween"):
            bounds = getattr(self, op)
            if bounds is not None and (len(bounds) != 2 or bounds[0] > bounds[1]):
                raise ValueError(f"{op} needs [low, high]")
        return self

    def operators(self) -> dict[str, Any]:
        return {op: getattr(self, op) for op in OPERATORS if getattr(self, op) is not None}

    def identity(self) -> str:
        """Stable handle used to pair rules between two contract versions."""
        if self.name:
            return self.name
        if self.type == "library":
            return f"metric:{self.metric}"
        return f"{self.type}:{' '.join((self.query or self.description or '').split())}"

    def evaluate(self, value: float) -> tuple[bool, str]:
        """Apply the rule's operators to an observed value. Returns (passed, expectation)."""
        ops = self.operators()
        passed = all(_holds(op, value, bound) for op, bound in ops.items())
        return passed, describe_operators(ops)


class Relationship(_Loose):
    type: str = "foreignKey"
    #: ``table.column`` or ``schema.table.column`` of the referenced column.
    to: str

    def target(self) -> tuple[str, str]:
        table, _, column = self.to.rpartition(".")
        if not table:
            raise ConfigError(f"relationship target must be table.column: {self.to!r}")
        return table, column


class Property(_Loose):
    name: str
    logicalType: str | None = None
    physicalType: str | None = None
    required: bool = False
    unique: bool = False
    primaryKey: bool = False
    quality: list[QualityRule] = []
    relationships: list[Relationship] = []


class SchemaObject(_Loose):
    name: str
    physicalName: str | None = None
    properties: list[Property] = []
    quality: list[QualityRule] = []

    @property
    def table(self) -> str:
        return self.physicalName or self.name


class Contract(_Loose):
    apiVersion: str | None = None
    kind: str | None = None
    id: str | None = None
    name: str | None = None
    version: str = "0.0.0"
    status: str | None = None
    objects: list[SchemaObject] = Field(default=[], validation_alias="schema")


def parse_contract(raw: Any, source: str = "<contract>") -> Contract:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: contract must be a YAML mapping")
    try:
        contract = Contract.model_validate(raw)
    except ValidationError as exc:
        lines = [f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ConfigError(f"{source}: invalid contract:\n" + "\n".join(lines)) from exc
    if not contract.objects:
        raise ConfigError(f"{source}: contract has no schema objects")
    return contract


def load_contract(path: str | Path) -> Contract:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"contract file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    return parse_contract(raw, str(path))
