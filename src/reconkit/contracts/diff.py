"""Classify the changes between two versions of a contract.

The viewpoint is the consumer's: a change is *breaking* when something a consumer could
rely on under the old contract is no longer promised by the new one. Tightening a
guarantee is non-breaking for consumers (it may still break the producer, which
``reconkit contract check`` will tell you).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from reconkit.contracts.model import (
    Contract,
    Property,
    QualityRule,
    SchemaObject,
    describe_operators,
)

Severity = Literal["breaking", "non_breaking"]


@dataclass(frozen=True)
class Change:
    severity: Severity
    path: str
    message: str


def _interval(rule: QualityRule) -> tuple[float, float] | None:
    """The closed interval of accepted values, when the operators describe one."""
    ops = rule.operators()
    if not ops or "mustNotBe" in ops or "mustNotBeBetween" in ops:
        return None
    lo, hi = -math.inf, math.inf
    for op, bound in ops.items():
        if op == "mustBe":
            lo, hi = max(lo, bound), min(hi, bound)
        elif op in ("mustBeGreaterThan", "mustBeGreaterOrEqualTo"):
            # Strict and non-strict bounds are treated alike; the difference is one ulp.
            lo = max(lo, bound)
        elif op in ("mustBeLessThan", "mustBeLessOrEqualTo"):
            hi = min(hi, bound)
        elif op == "mustBeBetween":
            lo, hi = max(lo, bound[0]), min(hi, bound[1])
    return lo, hi


def _diff_rule(path: str, old: QualityRule, new: QualityRule) -> list[Change]:
    changes: list[Change] = []
    if (old.type, old.metric, old.query, old.unit) != (new.type, new.metric, new.query, new.unit):
        return [
            Change(
                "breaking",
                path,
                "rule definition changed; cannot tell if it still guarantees what it did",
            )
        ]
    old_valid = old.arguments.get("validValues")
    new_valid = new.arguments.get("validValues")
    if old_valid != new_valid:
        added = sorted({str(v) for v in new_valid or []} - {str(v) for v in old_valid or []})
        removed = sorted({str(v) for v in old_valid or []} - {str(v) for v in new_valid or []})
        if added:
            changes.append(Change("breaking", path, f"accepted values added: {added}"))
        if removed:
            changes.append(Change("non_breaking", path, f"accepted values removed: {removed}"))
    elif old.arguments != new.arguments:
        changes.append(Change("breaking", path, "rule arguments changed"))
    if old.operators() != new.operators():
        a, b = _interval(old), _interval(new)
        was = describe_operators(old.operators())
        now = describe_operators(new.operators())
        if a is not None and b is not None and a[0] <= b[0] and b[1] <= a[1]:
            changes.append(Change("non_breaking", path, f"threshold tightened: {was} -> {now}"))
        else:
            changes.append(Change("breaking", path, f"threshold loosened or moved: {was} -> {now}"))
    return changes


def _diff_rules(path: str, old: list[QualityRule], new: list[QualityRule]) -> list[Change]:
    changes: list[Change] = []
    old_by = {r.identity(): r for r in old}
    new_by = {r.identity(): r for r in new}
    for key in old_by.keys() - new_by.keys():
        changes.append(Change("breaking", f"{path}.quality[{key}]", "quality rule removed"))
    for key in new_by.keys() - old_by.keys():
        changes.append(Change("non_breaking", f"{path}.quality[{key}]", "quality rule added"))
    for key in old_by.keys() & new_by.keys():
        changes.extend(_diff_rule(f"{path}.quality[{key}]", old_by[key], new_by[key]))
    return changes


def _diff_property(path: str, old: Property, new: Property) -> list[Change]:
    changes: list[Change] = []

    def norm(value: str | None) -> str | None:
        return value.lower().replace(" ", "") if value else None

    if norm(old.logicalType) != norm(new.logicalType):
        changes.append(
            Change("breaking", path, f"logicalType changed: {old.logicalType} -> {new.logicalType}")
        )
    if norm(old.physicalType) != norm(new.physicalType):
        changes.append(
            Change(
                "breaking",
                path,
                f"physicalType changed: {old.physicalType} -> "
                f"{new.physicalType} (treated as breaking; review if it is a "
                "pure widening)",
            )
        )
    for flag, label in (("required", "nullable"), ("unique", "non-unique")):
        was, now = getattr(old, flag), getattr(new, flag)
        if was and not now:
            changes.append(Change("breaking", path, f"{flag} dropped: column may now be {label}"))
        elif now and not was:
            changes.append(Change("non_breaking", path, f"{flag} added"))
    if old.primaryKey != new.primaryKey:
        changes.append(
            Change("breaking", path, f"primaryKey changed: {old.primaryKey} -> {new.primaryKey}")
        )
    old_rel, new_rel = {r.to for r in old.relationships}, {r.to for r in new.relationships}
    for to in sorted(old_rel - new_rel):
        changes.append(Change("breaking", path, f"relationship to {to} removed"))
    for to in sorted(new_rel - old_rel):
        changes.append(Change("non_breaking", path, f"relationship to {to} added"))
    changes.extend(_diff_rules(path, old.quality, new.quality))
    return changes


def _diff_object(old: SchemaObject, new: SchemaObject) -> list[Change]:
    path = old.name
    changes: list[Change] = []
    if old.table != new.table:
        changes.append(
            Change("breaking", path, f"physical table changed: {old.table} -> {new.table}")
        )
    old_props = {p.name.lower(): p for p in old.properties}
    new_props = {p.name.lower(): p for p in new.properties}
    for name in old_props.keys() - new_props.keys():
        changes.append(Change("breaking", f"{path}.{old_props[name].name}", "property removed"))
    for name in new_props.keys() - old_props.keys():
        changes.append(Change("non_breaking", f"{path}.{new_props[name].name}", "property added"))
    for name in old_props.keys() & new_props.keys():
        changes.extend(
            _diff_property(f"{path}.{old_props[name].name}", old_props[name], new_props[name])
        )
    changes.extend(_diff_rules(path, old.quality, new.quality))
    return changes


def diff_contracts(old: Contract, new: Contract) -> list[Change]:
    """All changes from ``old`` to ``new``, breaking first, then by path."""
    changes: list[Change] = []
    old_objs = {o.name.lower(): o for o in old.objects}
    new_objs = {o.name.lower(): o for o in new.objects}
    for name in old_objs.keys() - new_objs.keys():
        changes.append(Change("breaking", old_objs[name].name, "schema object removed"))
    for name in new_objs.keys() - old_objs.keys():
        changes.append(Change("non_breaking", new_objs[name].name, "schema object added"))
    for name in old_objs.keys() & new_objs.keys():
        changes.extend(_diff_object(old_objs[name], new_objs[name]))
    return sorted(changes, key=lambda c: (c.severity != "breaking", c.path, c.message))


def major_version(version: str) -> int | None:
    head = version.strip().lstrip("vV").split(".")[0]
    return int(head) if head.isdigit() else None


def version_bump_ok(old: Contract, new: Contract, changes: list[Change]) -> bool:
    """A breaking change must come with a major version bump (semantic versioning)."""
    if not any(c.severity == "breaking" for c in changes):
        return True
    a, b = major_version(old.version), major_version(new.version)
    return a is not None and b is not None and b > a
