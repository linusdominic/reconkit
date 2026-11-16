from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from reconkit.adapters import DbApiAdapter
from reconkit.contracts import check_contract, diff_contracts, load_contract
from reconkit.contracts.diff import version_bump_ok
from reconkit.contracts.model import parse_contract
from reconkit.errors import ConfigError

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)

CONTRACT: dict[str, Any] = {
    "apiVersion": "v3.1.0",
    "kind": "DataContract",
    "name": "loans",
    "version": "1.0.0",
    "team": {"name": "ignored, like other ODCS sections reconkit does not check"},
    "schema": [
        {
            "name": "loans",
            "physicalName": "main.loans",
            "properties": [
                {"name": "loan_id", "logicalType": "integer", "physicalType": "BIGINT",
                 "primaryKey": True, "required": True},
                {"name": "customer_id", "logicalType": "integer", "required": True,
                 "relationships": [{"type": "foreignKey", "to": "main.customers.customer_id"}]},
                {"name": "status", "logicalType": "string", "required": True,
                 "quality": [{"name": "known_status", "metric": "invalidValues",
                              "arguments": {"validValues": ["OPEN", "CLOSED"]}, "mustBe": 0}]},
                {"name": "amount", "logicalType": "number", "physicalType": "DECIMAL(12,2)",
                 "quality": [
                     {"metric": "nullValues", "unit": "percent", "mustBeLessThan": 50},
                     {"name": "positive", "type": "sql", "mustBe": 0,
                      "query": "SELECT COUNT(*) FROM ${object} WHERE ${property} < 0"},
                     {"type": "text", "description": "Amounts are in SGD."}]},
                {"name": "ref", "logicalType": "string", "unique": True,
                 "quality": [{"rule": "duplicateCount", "mustBe": 0},
                             {"metric": "missingValues", "mustBeLessOrEqualTo": 1}]},
                {"name": "loaded_at", "logicalType": "timestamp",
                 "quality": [{"metric": "freshness", "unit": "hours", "mustBeLessThan": 24}]},
                {"name": "payload", "logicalType": "object"},
            ],
            "quality": [
                {"metric": "rowCount", "mustBeBetween": [1, 100]},
                {"name": "no_future_loads", "type": "sql", "mustBe": 0,
                 "query": "SELECT COUNT(*) FROM {object} WHERE loaded_at > TIMESTAMP '2026-01-11'"},
            ],
        }
    ],
}  # fmt: skip


def seed(db: DbApiAdapter) -> None:
    db.execute("CREATE TABLE customers (customer_id BIGINT)")
    db.execute("INSERT INTO customers VALUES (1), (2)")
    db.execute(
        "CREATE TABLE loans (loan_id BIGINT, customer_id BIGINT, status VARCHAR, "
        "amount DECIMAL(12,2), ref VARCHAR, loaded_at TIMESTAMP, payload JSON, extra INTEGER)"
    )
    db.execute(
        "INSERT INTO loans VALUES "
        "(1, 1, 'OPEN', 10.00, 'a', TIMESTAMP '2026-01-10 02:00:00', '{}', 0),"
        "(2, 2, 'CLOSED', NULL, 'b', TIMESTAMP '2026-01-09 23:00:00', '{}', 0),"
        "(3, 1, 'OPEN', 5.50, '', TIMESTAMP '2026-01-10 03:00:00', '{}', 0)"
    )


def by_rule(result: Any) -> dict[tuple[str, str], Any]:
    return {(r.target, r.rule): r for r in result.results}


def test_contract_passes_on_conforming_table(duck: Callable[..., DbApiAdapter]) -> None:
    db = duck()
    seed(db)
    result = check_contract(db, parse_contract(CONTRACT), now=NOW)
    rules = by_rule(result)
    assert result.passed, [r for r in result.results if r.status in ("fail", "error")]
    assert rules[("loans.loaded_at", "freshness")].observed.startswith("9.00 hours old")
    assert rules[("loans.amount", "nullValues")].observed == "33.33% (1 of 3)"
    assert rules[("loans.amount", "text")].status == "skip"
    assert rules[("loans.payload", "logicalType")].status == "skip"
    assert rules[("loans.extra", "declared in contract")].status == "warn"
    assert rules[("loans", "rowCount")].expected == "mustBeBetween [1, 100]"
    assert result.count("fail") == 0 and result.count("warn") == 1
    assert result.to_dict()["passed"] is True
    # Schema checks, implied required/unique checks and declared rules were all evaluated.
    assert {r for _, r in rules} >= {"table exists", "column exists", "physicalType", "required",
                                     "unique", "known_status", "positive", "no_future_loads",
                                     "references main.customers.customer_id"}  # fmt: skip


def test_contract_reports_each_kind_of_violation(duck: Callable[..., DbApiAdapter]) -> None:
    db = duck()
    seed(db)
    db.execute(
        "INSERT INTO loans VALUES "
        "(3, 9, 'WEIRD', -1.00, 'a', TIMESTAMP '2026-01-12 00:00:00', '{}', 0),"
        "(NULL, 1, NULL, NULL, NULL, NULL, '{}', 0)"
    )
    db.execute("ALTER TABLE loans DROP COLUMN payload")
    db.execute("ALTER TABLE loans ALTER amount TYPE DOUBLE")
    result = check_contract(db, parse_contract(CONTRACT), now=NOW)
    failed = {(r.target, r.rule): r.observed for r in result.results if r.status == "fail"}
    assert failed == {
        ("loans.payload", "column exists"): "",
        ("loans.amount", "physicalType"): "DOUBLE",
        ("loans.loan_id", "required"): "1",
        ("loans.loan_id", "unique"): "1",
        ("loans.status", "required"): "1",
        ("loans.status", "known_status"): "1",
        ("loans.amount", "positive"): "1",
        ("loans.ref", "unique"): "1",
        ("loans.ref", "duplicateValues"): "1",
        ("loans.ref", "missingValues"): "2",
        ("loans.customer_id", "references main.customers.customer_id"): "1 orphaned",
        ("loans", "no_future_loads"): "1",
    }
    assert not result.passed


def test_contract_stale_table_empty_table_and_missing_table(
    duck: Callable[..., DbApiAdapter],
) -> None:
    db = duck()
    seed(db)
    late = datetime(2026, 1, 20, tzinfo=timezone.utc)
    stale = by_rule(check_contract(db, parse_contract(CONTRACT), now=late))
    assert stale[("loans.loaded_at", "freshness")].status == "fail"
    db.execute("DELETE FROM loans")
    empty = by_rule(check_contract(db, parse_contract(CONTRACT), now=NOW))
    assert empty[("loans", "rowCount")].status == "fail"
    assert empty[("loans.loaded_at", "freshness")].observed == "no rows"
    missing = check_contract(db, parse_contract(CONTRACT), table="main.nope", now=NOW)
    assert [(r.rule, r.status) for r in missing.results] == [("table exists", "fail")]


def test_contract_composite_key_logical_type_mismatch_and_rule_errors(
    duck: Callable[..., DbApiAdapter],
) -> None:
    db = duck()
    db.execute("CREATE TABLE s (a INTEGER, b INTEGER, c VARCHAR, d DATE)")
    db.execute(
        "INSERT INTO s VALUES (1, 1, 'x', DATE '2026-01-10'), (1, 1, 'y', DATE '2026-01-01'), (1, 2, 'z', NULL)"
    )
    contract = parse_contract({
        "version": "1.0.0",
        "schema": [{"name": "s", "properties": [
            {"name": "a", "logicalType": "integer", "primaryKey": True},
            {"name": "b", "logicalType": "string", "primaryKey": True},
            {"name": "c", "logicalType": "string", "quality": [
                {"metric": "invalidValues", "mustBe": 0},
                {"metric": "freshness", "mustBeLessThan": 1},
                {"metric": "nullValues"},
                {"type": "sql", "query": "SELECT nope FROM ${object}", "mustBe": 0},
                {"type": "custom", "engine": "soda"}]},
            {"name": "d", "logicalType": "date", "quality": [
                {"metric": "freshness", "unit": "days", "mustBeLessThan": 2},
                {"metric": "freshness", "unit": "fortnights", "mustBeLessThan": 2}],
             "relationships": [{"to": "nowhere"}]},
        ], "quality": [{"metric": "nullValues", "mustBe": 0},
                       {"type": "sql", "query": "SELECT COUNT(*) FROM ${property}", "mustBe": 0}]}],
    })  # fmt: skip
    result = check_contract(db, contract, now=NOW)
    status = {(r.target, r.rule): r.status for r in result.results}
    assert status[("s", "primary key (a, b) unique")] == "fail"
    assert status[("s.b", "logicalType")] == "fail"
    assert status[("s.c", "invalidValues")] == "error"  # no validValues given
    assert status[("s.c", "nullValues")] == "error"  # no operator
    assert status[("s.c", "sql")] == "error"  # query fails
    assert status[("s.c", "custom")] == "skip"
    assert status[("s.d", "references nowhere")] == "error"
    assert status[("s", "nullValues")] == "error"  # column metric at object level
    assert status[("s", "sql")] == "error"  # property placeholder at object level
    freshness = [r for r in result.results if r.rule == "freshness"]
    assert [r.status for r in freshness] == ["error", "pass", "error"]
    with pytest.raises(ConfigError, match="exactly one schema object"):
        two = parse_contract({"schema": [{"name": "a"}, {"name": "b"}]})
        check_contract(db, two, table="x")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "must be a YAML mapping"),
        ({"schema": []}, "no schema objects"),
        (
            {"schema": [{"name": "t", "quality": [{"metric": "vibes", "mustBe": 0}]}]},
            "unsupported metric",
        ),
        ({"schema": [{"name": "t", "quality": [{"type": "library"}]}]}, "needs a 'metric'"),
        ({"schema": [{"name": "t", "quality": [{"type": "sql"}]}]}, "needs a 'query'"),
        (
            {
                "schema": [
                    {"name": "t", "quality": [{"metric": "rowCount", "mustBeBetween": [5, 1]}]}
                ]
            },
            "low, high",
        ),
        ({"schema": [{"properties": []}]}, "name"),
    ],
)
def test_invalid_contracts(raw: Any, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_contract(raw)


def test_load_contract_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_contract(tmp_path / "nope.yml")
    bad = tmp_path / "bad.yml"
    bad.write_text("{")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_contract(bad)


@pytest.mark.parametrize(
    ("op", "bound", "value", "passed"),
    [
        ("mustBe", 0, 0, True), ("mustBe", 0, 1, False), ("mustNotBe", 0, 1, True),
        ("mustBeGreaterThan", 5, 5, False), ("mustBeGreaterOrEqualTo", 5, 5, True),
        ("mustBeLessThan", 5, 5, False), ("mustBeLessOrEqualTo", 5, 5, True),
        ("mustBeBetween", [1, 3], 3, True), ("mustBeBetween", [1, 3], 4, False),
        ("mustNotBeBetween", [1, 3], 2, False), ("mustNotBeBetween", [1, 3], 4, True),
    ],
)  # fmt: skip
def test_operators(op: str, bound: Any, value: float, passed: bool) -> None:
    contract = parse_contract(
        {"schema": [{"name": "t", "quality": [{"metric": "rowCount", op: bound}]}]}
    )
    assert contract.objects[0].quality[0].evaluate(value)[0] is passed


# ---- contract diff -----------------------------------------------------------------------


def changed(
    mutate: Callable[[dict[str, Any]], None], version: str = "1.1.0"
) -> list[tuple[str, str, str]]:
    new = copy.deepcopy(CONTRACT)
    new["version"] = version
    mutate(new)
    changes = diff_contracts(parse_contract(CONTRACT), parse_contract(new))
    return [(c.severity, c.path, c.message) for c in changes]


def props(c: dict[str, Any]) -> list[dict[str, Any]]:
    return c["schema"][0]["properties"]  # type: ignore[no-any-return]


def test_identical_contracts_have_no_changes() -> None:
    assert changed(lambda c: None) == []


@pytest.mark.parametrize(
    ("mutate", "severity", "path", "fragment"),
    [
        (lambda c: props(c).pop(6), "breaking", "loans.payload", "property removed"),
        (lambda c: props(c).append({"name": "new_col"}), "non_breaking", "loans.new_col", "added"),
        (lambda c: props(c)[0].update(logicalType="string"), "breaking", "loans.loan_id", "logicalType"),
        (lambda c: props(c)[0].update(physicalType="INTEGER"), "breaking", "loans.loan_id", "physicalType"),
        (lambda c: props(c)[0].update(physicalType="bigint"), None, "", ""),
        (lambda c: props(c)[1].update(required=False), "breaking", "loans.customer_id", "required dropped"),
        (lambda c: props(c)[3].update(required=True), "non_breaking", "loans.amount", "required added"),
        (lambda c: props(c)[4].update(unique=False), "breaking", "loans.ref", "unique dropped"),
        (lambda c: props(c)[0].update(primaryKey=False), "breaking", "loans.loan_id", "primaryKey"),
        (lambda c: props(c)[1].update(relationships=[]), "breaking", "loans.customer_id", "relationship"),
        (lambda c: props(c)[0].update(relationships=[{"to": "x.y"}]), "non_breaking", "loans.loan_id", "relationship to x.y added"),
        (lambda c: props(c)[2]["quality"][0]["arguments"]["validValues"].append("WRITTEN_OFF"),
         "breaking", "loans.status.quality[known_status]", "accepted values added"),
        (lambda c: props(c)[2]["quality"][0]["arguments"]["validValues"].remove("CLOSED"),
         "non_breaking", "loans.status.quality[known_status]", "accepted values removed"),
        (lambda c: props(c)[2].update(quality=[]), "breaking", "loans.status.quality[known_status]", "removed"),
        (lambda c: props(c)[5]["quality"][0].update(mustBeLessThan=12), "non_breaking",
         "loans.loaded_at.quality[metric:freshness]", "tightened"),
        (lambda c: props(c)[5]["quality"][0].update(mustBeLessThan=48), "breaking",
         "loans.loaded_at.quality[metric:freshness]", "loosened"),
        (lambda c: props(c)[5]["quality"][0].update(unit="days"), "breaking",
         "loans.loaded_at.quality[metric:freshness]", "definition changed"),
        (lambda c: c["schema"][0]["quality"][0].update(mustBeBetween=[10, 50]), "non_breaking",
         "loans.quality[metric:rowCount]", "tightened"),
        (lambda c: c["schema"][0]["quality"][0].update(mustBeBetween=[0, 100]), "breaking",
         "loans.quality[metric:rowCount]", "loosened"),
        (lambda c: c["schema"][0]["quality"].append({"metric": "rowCount", "name": "more", "mustBe": 3}),
         "non_breaking", "loans.quality[more]", "added"),
        (lambda c: c["schema"][0].update(physicalName="main.loans_v2"), "breaking", "loans", "physical table"),
        (lambda c: c["schema"].append({"name": "payments"}), "non_breaking", "payments", "object added"),
        (lambda c: c["schema"][0].update(name="renamed"), "breaking", "loans", "object removed"),
        (lambda c: props(c)[4]["quality"][1].update(arguments={"missingValues": ["", "n/a"]}),
         "breaking", "loans.ref.quality[metric:missingValues]", "arguments changed"),
    ],
)  # fmt: skip
def test_change_classification(mutate: Any, severity: str | None, path: str, fragment: str) -> None:
    changes = changed(mutate)
    if severity is None:
        assert changes == []
        return
    assert any(s == severity and p == path and fragment in m for s, p, m in changes), changes


def test_breaking_changes_sort_first_and_need_a_major_bump() -> None:
    def mutate(c: dict[str, Any]) -> None:
        props(c).append({"name": "aaa_new"})
        props(c).pop(6)

    old = parse_contract(CONTRACT)
    for version, ok in (("1.1.0", False), ("2.0.0", True), ("v2", True), ("next", False)):
        new_raw = copy.deepcopy(CONTRACT)
        new_raw["version"] = version
        mutate(new_raw)
        changes = diff_contracts(old, parse_contract(new_raw))
        assert [c.severity for c in changes] == ["breaking", "non_breaking"]
        assert version_bump_ok(old, parse_contract(new_raw), changes) is ok
    assert version_bump_ok(old, old, [])
