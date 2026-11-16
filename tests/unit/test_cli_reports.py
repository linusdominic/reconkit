"""End-to-end through the CLI, using two DuckDB files as the two 'systems'."""

from __future__ import annotations

import json
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from click.testing import CliRunner
from rich.console import Console

from reconkit.cli import main
from reconkit.config import load_config
from reconkit.report import render_terminal, to_html, to_json, to_junit
from reconkit.runner import Runner, run_config

CONTRACT = {
    "name": "loans",
    "version": "1.0.0",
    "schema": [
        {
            "name": "loans",
            "physicalName": "loans",
            "properties": [
                {"name": "loan_id", "logicalType": "integer", "primaryKey": True, "required": True},
                {"name": "status", "logicalType": "string", "required": True,
                 "quality": [{"name": "known_status", "metric": "invalidValues", "mustBe": 0,
                              "arguments": {"validValues": ["OPEN", "CLOSED"]}}]},
                {"name": "amount", "logicalType": "number"},
            ],
        }
    ],
}  # fmt: skip


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Source and warehouse DuckDB files with a known set of differences."""
    with duckdb.connect(str(tmp_path / "src.duckdb")) as src:
        src.execute("CREATE TABLE loans (loan_id BIGINT, status VARCHAR, amount DECIMAL(12,2))")
        src.execute("INSERT INTO loans SELECT i, 'OPEN', i * 1.5 FROM range(1, 1001) t(i)")
        src.execute("CREATE TABLE loans_copy AS SELECT * FROM loans")
    with duckdb.connect(str(tmp_path / "wh.duckdb")) as wh:
        wh.execute("CREATE TABLE loans (loan_id BIGINT, status VARCHAR, amount DECIMAL(12,2))")
        wh.execute(
            "INSERT INTO loans SELECT i, 'OPEN', i * 1.5 FROM range(1, 1001) t(i) WHERE i <> 17"
        )
        wh.execute("UPDATE loans SET amount = amount + 5 WHERE loan_id = 400")
        wh.execute("UPDATE loans SET status = '<b>odd</b>' WHERE loan_id = 401")
        wh.execute("INSERT INTO loans VALUES (5000, 'OPEN', 1)")
    (tmp_path / "contract.yml").write_text(yaml.safe_dump(CONTRACT))
    proposed = json.loads(json.dumps(CONTRACT))
    proposed["schema"][0]["properties"].pop()
    (tmp_path / "proposed.yml").write_text(yaml.safe_dump(proposed))
    config = {
        "connections": {
            "src": {"type": "duckdb", "path": "src.duckdb"},
            "wh": {"type": "duckdb", "path": "wh.duckdb"},
        },
        "audit": {"connection": "wh", "table": "recon_audit"},
        "checks": {
            "loans": {
                "description": "source vs warehouse",
                "left": {"connection": "src", "table": "loans"},
                "right": {"connection": "wh", "table": "loans"},
                "key": "loan_id",
            },
            "loans_copy": {
                "left": {"connection": "src", "table": "loans"},
                "right": {"connection": "src", "table": "loans_copy"},
                "key": ["loan_id"],
            },
        },
        "contracts": {"wh_contract": {"connection": "wh", "contract": "contract.yml"}},
    }
    (tmp_path / "reconkit.yml").write_text(yaml.safe_dump(config))
    return tmp_path


def invoke(*args: str) -> Any:
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_run_all_reports_and_audit(project: Path) -> None:
    cfg = str(project / "reconkit.yml")
    out = {k: str(project / f"r.{k}") for k in ("json", "xml", "html")}
    result = invoke("run", "-c", cfg, "--all", "--json", out["json"], "--junit", out["xml"],
                    "--html", out["html"])  # fmt: skip
    assert result.exit_code == 1, result.output
    assert "FAIL  loans" in result.output
    assert "PASS  loans_copy" in result.output
    assert "1 missing in left, 1 missing in right, 2 changed" in result.output
    assert "3 check(s): 1 passed, 2 failed, 0 errored" in result.output

    report = json.loads(Path(out["json"]).read_text())
    assert report["exit_code"] == 1
    assert report["summary"] == {"pass": 1, "fail": 2, "error": 0}
    loans, copy, contract = report["checks"]
    assert loans["diff"]["summary"]["changed_by_column"] == {"amount": 1, "status": 1}
    assert loans["diff"]["strategy"] == "bisect/range"
    assert loans["audit_rows_written"] == 4
    assert [r["kind"] for r in loans["diff"]["rows"]] == [
        "missing_in_right", "changed", "changed", "missing_in_left",
    ]  # fmt: skip
    assert loans["diff"]["rows"][1]["key"] == {"loan_id": "400"}
    assert copy["diff"]["strategy"] == "join" and copy["diff"]["match"] is True
    assert contract["contract"]["summary"]["fail"] == 1

    suite = ET.parse(out["xml"]).getroot().find("testsuite")
    assert suite is not None
    assert suite.get("failures") == "2" and suite.get("errors") == "0"
    cases = {(c.get("classname"), c.get("name")): c for c in suite.findall("testcase")}
    failure = cases[("reconkit.reconcile", "loans")].find("failure")
    assert failure is not None and "changed [loan_id=400] amount: '600.00' -> '605.00'" in (
        failure.text or ""
    )
    assert (
        cases[("reconkit.contract.wh_contract", "loans.status: known_status")].find("failure")
        is not None
    )
    assert cases[("reconkit.reconcile", "loans_copy")].find("failure") is None

    html = Path(out["html"]).read_text()
    assert html.startswith("<!doctype html>")
    assert "&lt;b&gt;odd&lt;/b&gt;" in html and "<b>odd</b>" not in html  # values are escaped
    assert "http" not in html and "<script" not in html  # self-contained

    with duckdb.connect(str(project / "wh.duckdb")) as wh:
        runs = wh.execute(
            "SELECT check_name, status, missing_in_left, missing_in_right, changed "
            "FROM recon_audit_runs ORDER BY 1"
        ).fetchall()
        diffs = wh.execute(
            "SELECT diff_kind, row_key, column_name, left_value, right_value FROM recon_audit ORDER BY 2, 3"
        ).fetchall()
    assert runs == [("loans", "mismatch", 1, 1, 2), ("loans_copy", "match", 0, 0, 0)]
    assert ("changed", '{"loan_id": "400"}', "amount", "600.00", "605.00") in diffs
    assert ("missing_in_right", '{"loan_id": "17"}', None, None, None) in diffs
    assert len(diffs) == 4


def test_run_named_check_exit_codes_and_no_audit(project: Path) -> None:
    cfg = str(project / "reconkit.yml")
    ok = invoke("run", "-c", cfg, "loans_copy", "--no-audit", "-q")
    assert ok.exit_code == 0 and ok.output == ""
    with duckdb.connect(str(project / "wh.duckdb")) as wh:
        assert wh.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name LIKE 'recon%'"
        ).fetchone() == (0,)
    stdout_json = invoke("run", "-c", cfg, "loans", "--no-audit", "--json", "-")
    assert json.loads(stdout_json.output)["checks"][0]["status"] == "fail"
    assert invoke("run", "-c", cfg).exit_code == 2  # neither names nor --all
    assert invoke("run", "-c", cfg, "--all", "loans").exit_code == 2
    unknown = invoke("run", "-c", cfg, "nope")
    assert unknown.exit_code == 2 and "unknown check" in unknown.output
    assert invoke("run", "-c", str(project / "missing.yml"), "--all").exit_code == 2


def test_runtime_errors_exit_3_and_do_not_stop_other_checks(project: Path) -> None:
    cfg_path = project / "reconkit.yml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["checks"]["broken"] = {
        "left": {"connection": "src", "table": "no_such_table"},
        "right": {"connection": "wh", "table": "loans"},
        "key": "loan_id",
    }
    cfg["contracts"]["missing_contract"] = {"connection": "wh", "contract": "nope.yml"}
    cfg_path.write_text(yaml.safe_dump(cfg))
    result = invoke(
        "run", "-c", str(cfg_path), "--all", "--no-audit", "--junit", str(project / "r.xml")
    )
    assert result.exit_code == 3
    assert "ERROR broken" in result.output and "PASS  loans_copy" in result.output
    suite = ET.parse(project / "r.xml").getroot().find("testsuite")
    assert suite is not None and suite.get("errors") == "2"


def test_adhoc_diff_command(project: Path) -> None:
    cfg = str(project / "reconkit.yml")
    result = invoke("diff", "-c", cfg, "--left", "src:loans", "--right", "wh:loans", "-k", "loan_id",
                    "--where", "loan_id < 300", "--ignore", "status", "--bucketing", "hash")  # fmt: skip
    assert result.exit_code == 1
    assert "0 missing in left, 1 missing in right, 0 changed" in result.output
    assert "bisect/hash" in result.output
    assert invoke("diff", "-c", cfg, "--left", "src:loans", "--right", "src:loans_copy", "-k",
                  "loan_id", "--column", "amount", "--sample", "0.5", "--limit", "5").exit_code == 0  # fmt: skip
    assert (
        invoke(
            "diff", "-c", cfg, "--left", "loans", "--right", "wh:loans", "-k", "loan_id"
        ).exit_code
        == 2
    )
    assert (
        invoke(
            "diff", "-c", cfg, "--left", "zz:loans", "--right", "wh:loans", "-k", "loan_id"
        ).exit_code
        == 2
    )


def test_validate_command(project: Path) -> None:
    cfg = str(project / "reconkit.yml")
    result = invoke("validate", "-c", cfg, "--connect")
    assert result.exit_code == 0
    assert "2 connections, 2 checks, 1 contracts" in result.output
    assert "connection wh: ok" in result.output
    (project / "bad.yml").write_text("checks: {x: {left: 1}}")
    bad = invoke("validate", "-c", str(project / "bad.yml"))
    assert bad.exit_code == 2 and "configuration error" in bad.output


def test_contract_check_command(project: Path) -> None:
    cfg = str(project / "reconkit.yml")
    failing = invoke(
        "contract", "check", str(project / "contract.yml"), "-c", cfg, "--connection", "wh"
    )
    assert failing.exit_code == 1 and "known_status" in failing.output
    passing = invoke("contract", "check", str(project / "contract.yml"), "-c", cfg, "--connection",
                     "src", "--table", "loans_copy", "-v")  # fmt: skip
    assert passing.exit_code == 0 and "column exists" in passing.output
    assert (
        invoke(
            "contract", "check", str(project / "contract.yml"), "-c", cfg, "--connection", "zz"
        ).exit_code
        == 2
    )


def test_contract_diff_command(project: Path) -> None:
    old, new = str(project / "contract.yml"), str(project / "proposed.yml")
    text = invoke("contract", "diff", old, new)
    assert text.exit_code == 1
    assert (
        "1 breaking, 0 non-breaking" in text.output
        and "loans.amount: property removed" in text.output
    )
    assert "without a major version bump" in text.output
    assert invoke("contract", "diff", old, old).exit_code == 0
    assert invoke("contract", "diff", old, new, "--fail-on", "never").exit_code == 0
    assert invoke("contract", "diff", old, new, "--require-major-bump").exit_code == 1
    as_json = json.loads(invoke("contract", "diff", old, new, "--format", "json").output)
    assert as_json["breaking"] == 1 and as_json["major_version_bumped"] is False
    markdown = invoke("contract", "diff", old, new, "--format", "markdown").output
    assert "| **breaking** | `loans.amount` | property removed |" in markdown
    assert "No changes" in invoke("contract", "diff", old, old, "--format", "markdown").output
    assert invoke("contract", "diff", old, str(project / "nope.yml")).exit_code == 2
    assert invoke("--version").output.startswith("reconkit, version")


def test_renderers_cover_errors_warnings_and_truncation(project: Path) -> None:
    config = load_config(project / "reconkit.yml")
    config.checks["loans"].ignore = ["status"]
    config.checks["loans"].options.sample = 0.9
    config.checks["loans"].tolerances = {}
    with Runner(config) as runner:
        report = runner.run(audit=False)
    console = Console(record=True, width=160)
    render_terminal(report, console, max_rows=1, verbose=True)
    text = console.export_text()
    assert "more differing rows" in text and "sample 0.9" in text
    assert "pass" in text and "column exists" in text
    assert json.loads(to_json(report, max_rows=1))["checks"][0]["diff"]["rows_listed"] == 1
    assert "sample rate 0.9" in to_html(report, max_rows=1)
    assert "... " in to_junit(report, max_rows=1)
    render_terminal(report, Console(record=True), max_rows=0)


# ---- orchestrator integrations ---------------------------------------------------------


def test_airflow_operator_without_airflow(project: Path) -> None:
    from reconkit.integrations.airflow import AirflowException, ReconkitOperator

    cfg = str(project / "reconkit.yml")
    ok = ReconkitOperator(task_id="t", config_path=cfg, checks=["loans_copy"], audit=False)
    summary = ok.execute({})
    assert ok.task_id == "t"
    assert summary["exit_code"] == 0 and summary["checks"]["loans_copy"]["status"] == "pass"

    failing = ReconkitOperator(task_id="t", config_path=cfg, checks=["loans"], audit=False,
                               json_path=str(project / "airflow.json"))  # fmt: skip
    with pytest.raises(AirflowException, match="checks failed: \\['loans'\\]"):
        failing.execute({})
    assert json.loads((project / "airflow.json").read_text())["exit_code"] == 1

    tolerant = ReconkitOperator(task_id="t", config_path=cfg, checks=["loans"], audit=False,
                                fail_on_diff=False)  # fmt: skip
    assert tolerant.execute({})["exit_code"] == 1

    raw = yaml.safe_load((project / "reconkit.yml").read_text())
    raw["checks"]["loans"]["left"]["table"] = "gone"
    (project / "reconkit.yml").write_text(yaml.safe_dump(raw))
    with pytest.raises(AirflowException, match="could not run"):
        tolerant.execute({})


def test_dagster_helper_with_a_fake_dagster(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reconkit.integrations.dagster import build_asset_check, outcome_metadata

    fake = types.ModuleType("dagster")

    class AssetCheckResult:
        def __init__(self, *, passed: bool, metadata: dict[str, Any]) -> None:
            self.passed, self.metadata = passed, metadata

    def asset_check(**decorator_kwargs: Any) -> Any:
        def wrap(fn: Any) -> Any:
            fn.decorator_kwargs = decorator_kwargs
            return fn

        return wrap

    fake.AssetCheckResult = AssetCheckResult  # type: ignore[attr-defined]
    fake.asset_check = asset_check  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dagster", fake)

    cfg = str(project / "reconkit.yml")
    check = build_asset_check(asset="warehouse_loans", config_path=cfg, check="loans", audit=False)
    assert check.decorator_kwargs == {
        "asset": "warehouse_loans",
        "name": "reconkit_loans",
        "blocking": False,
    }
    result = check()
    assert result.passed is False
    assert result.metadata["missing_in_right"] == 1 and result.metadata["changed"] == 2
    assert result.metadata["strategy"] == "bisect/range"

    contract = build_asset_check(
        asset="a", config_path=cfg, check="wh_contract", name="c", blocking=True
    )()
    assert contract.passed is False and contract.metadata["rules_failed"] == 1

    report = run_config(cfg, ["loans_copy"], audit=False)
    assert outcome_metadata(report.outcomes[0])["status"] == "pass"
    raw = yaml.safe_load((project / "reconkit.yml").read_text())
    raw["checks"]["loans"]["left"]["table"] = "gone"
    (project / "reconkit.yml").write_text(yaml.safe_dump(raw))
    errored = build_asset_check(asset="a", config_path=cfg, check="loans")()
    assert errored.passed is False and "not found" in errored.metadata["error"]


def test_adapter_registry() -> None:
    from reconkit.adapters import create_adapter, register
    from reconkit.errors import ConfigError
    from tests.conftest import make_duck

    with pytest.raises(ConfigError, match="unknown connection type 'oracle'"):
        create_adapter("oracle", {})
    register("mydb", lambda config, name: make_duck(name))
    adapter = create_adapter("MyDB", {}, "custom")
    assert adapter.name == "custom" and adapter.query("SELECT 41 + 1") == [(42,)]
    adapter.close()
