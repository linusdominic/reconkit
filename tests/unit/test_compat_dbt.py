"""``reconkit data-diff --dbt``: project files, model selection, keys, prod paths, output."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import duckdb
import pytest
from click.testing import CliRunner

from reconkit.cli import main
from reconkit.compat import dbt
from reconkit.compat.uri import Connection
from reconkit.errors import ConfigError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "data_diff_jaffle_shop"


@pytest.fixture
def jaffle(tmp_path: Path) -> Path:
    """data-diff's dbt test project, with its database rebuilt from SQL."""
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project, ignore=shutil.ignore_patterns("build.sql", "NOTICE.md"))
    db = tmp_path / "jaffle_shop.duckdb"  # the views refer to the catalog by this name
    with duckdb.connect(str(db)) as con:
        con.execute((FIXTURE / "build.sql").read_text(encoding="utf-8"))
    (project / "profiles.yml").write_text(
        "jaffle_shop:\n  target: dev\n  outputs:\n    dev:\n"
        f"      type: duckdb\n      path: \"{{{{ env_var('JAFFLE_DB', '{db}') }}}}\"\n"
        "      schema: dev\n",
        encoding="utf-8",
    )
    return project


def _squash(text: str) -> str:
    return "".join(line.strip() for line in text.split("\n")).replace(" ", "")


# Expected output copied from data-diff's tests/test_dbt.py::test_integration_basic_dbt
# (MIT, see tests/fixtures/data_diff_jaffle_shop/NOTICE.md), compared the way that test
# compares it: whitespace removed, each block a substring of the output.
DATA_DIFF_EXPECTED = [
    """
    jaffle_shop.prod.orders <> jaffle_shop.dev.orders
    Primary Keys: ['order_id']
    Where Filter: 'amount >= 0'
    Included Columns: ['order_id', 'customer_id', 'order_date', 'amount', 'credit_card_amount', 'coupon_amount',
    'bank_transfer_amount', 'gift_card_amount']
    Excluded Columns: ['new_column']
    Columns removed [-1]: {'status'}
    Columns added [+1]: {'new_column'}
    Type changed [1]: {'order_date'}

    rows       PROD    <>            DEV
    ---------  ------  ------------  -----------------
    Total      10                    11 [+1]
    Added              +2
    Removed            -1
    Different          9
    Unchanged          0

    columns                 # diff values
    --------------------  ---------------
    amount                              8
    bank_transfer_amount                3
    coupon_amount                       3
    credit_card_amount                  6
    customer_id                         9
    gift_card_amount                    2
    """,
    """
    jaffle_shop.prod.stg_payments <> jaffle_shop.dev.stg_payments
    Primary Keys: ['payment_id']
    No row differences
    """,
    """
    jaffle_shop.prod.stg_customers <> jaffle_shop.dev.stg_customers
    Primary Keys: ['customer_id']
    No row differences
    """,
    """
    jaffle_shop.prod.stg_orders <> jaffle_shop.dev.stg_orders
    Primary Keys: ['order_id']
    No row differences
    """,
    """
    jaffle_shop.prod.customers <> jaffle_shop.dev.customers
    Primary Keys: ['customer_id']
    No row differences
    """,
]


def _squash_table_rules(text: str) -> str:
    # data-diff's tabulate rules are as wide as its columns; ours are as wide as ours.
    return "\n".join(
        "-" if set(line.strip()) <= {"-", " "} and line.strip() else line
        for line in text.split("\n")
    )


def test_matches_data_diffs_own_expected_output(jaffle: Path) -> None:
    result = CliRunner().invoke(
        main,
        ["data-diff", "--dbt", "--dbt-project-dir", str(jaffle), "--dbt-profiles-dir", str(jaffle)],
    )
    assert result.exit_code == 0, result.output
    actual = _squash(_squash_table_rules(result.stdout))
    for block in DATA_DIFF_EXPECTED:
        assert _squash(_squash_table_rules(block)) in actual, block
    assert result.stdout.count("No row differences") == 4


def test_fail_on_diff_and_select(jaffle: Path) -> None:
    base = ["data-diff", "--dbt", "--dbt-project-dir", str(jaffle)]
    result = CliRunner().invoke(
        main, [*base, "--select", "stg_orders stg_payments", "--fail-on-diff"]
    )
    assert result.exit_code == 0, result.output
    assert "orders <> " in result.stdout and "customers" not in result.stdout
    result = CliRunner().invoke(main, [*base, "--select", "orders", "--fail-on-diff"])
    assert result.exit_code == 1
    # --columns replaces the model's include and exclude lists.
    result = CliRunner().invoke(main, [*base, "--select", "orders", "-c", "amount"])
    values = result.stdout.split("# diff values")[1].strip().splitlines()[1:]
    assert [line.split() for line in values] == [["amount", "8"]]


def test_prod_schema_override_and_where(jaffle: Path) -> None:
    result = CliRunner().invoke(
        main,
        [
            "data-diff",
            "--dbt",
            "--dbt-project-dir",
            str(jaffle),
            "--select",
            "orders",
            "-ps",
            "dev",
        ],
    )
    assert "jaffle_shop.dev.orders <> jaffle_shop.dev.orders" in result.stdout
    assert "No row differences" in result.stdout


def test_state_manifest_gives_prod_locations(jaffle: Path, tmp_path: Path) -> None:
    manifest = json.loads((jaffle / "target" / "manifest.json").read_text())
    for node in manifest["nodes"].values():
        if node.get("resource_type") == "model":
            node["schema"] = "prod"
    state = tmp_path / "prod_manifest.json"
    state.write_text(json.dumps(manifest))
    result = CliRunner().invoke(
        main,
        [
            "data-diff",
            "--dbt",
            "--dbt-project-dir",
            str(jaffle),
            "--state",
            str(state),
            "--select",
            "customers",
        ],
    )
    assert "jaffle_shop.prod.customers <> jaffle_shop.dev.customers" in result.stdout


def test_needs_a_production_location(jaffle: Path) -> None:
    text = (jaffle / "dbt_project.yml").read_text()
    (jaffle / "dbt_project.yml").write_text(
        text.replace("prod_schema: prod", "").replace("prod_database: jaffle_shop", "")
    )
    result = CliRunner().invoke(main, ["data-diff", "--dbt", "--dbt-project-dir", str(jaffle)])
    assert result.exit_code == 2 and "no production location" in result.stderr


def test_dbt_rejects_positional_arguments(jaffle: Path) -> None:
    result = CliRunner().invoke(main, ["data-diff", "--dbt", "duckdb://x", "t", "duckdb://y", "t"])
    assert result.exit_code == 2


# -- pieces ---------------------------------------------------------------------------------


def _project(nodes: dict[str, dict[str, Any]], **extra: Any) -> dbt.DbtProject:
    manifest = {"nodes": nodes, **extra}
    return dbt.DbtProject(Path("."), "p", Path("target"), dbt.ProdSettings(), manifest)


def _model(name: str, **kw: Any) -> dict[str, Any]:
    node = {
        "resource_type": "model",
        "unique_id": f"model.p.{name}",
        "name": name,
        "fqn": ["p", *kw.pop("folders", []), name],
        "original_file_path": kw.pop("path", f"models/{name}.sql"),
        "tags": kw.pop("tags", []),
        "database": "db",
        "schema": "dev",
        "columns": kw.pop("columns", {}),
        "config": kw.pop("config", {}),
    }
    node.update(kw)
    return node


def test_selection_syntax() -> None:
    nodes = {
        n["unique_id"]: n
        for n in (
            _model("stg_a", tags=["nightly"], folders=["staging"], path="models/staging/stg_a.sql"),
            _model("stg_b", folders=["staging"], path="models/staging/stg_b.sql"),
            _model("mart", tags=["nightly"]),
            _model("report"),
        )
    }
    parents = {
        "model.p.mart": ["model.p.stg_a", "model.p.stg_b"],
        "model.p.report": ["model.p.mart"],
        "model.p.stg_a": [],
        "model.p.stg_b": [],
    }
    children = {
        "model.p.stg_a": ["model.p.mart"],
        "model.p.stg_b": ["model.p.mart"],
        "model.p.mart": ["model.p.report"],
        "model.p.report": [],
    }
    project = _project(nodes, parent_map=parents, child_map=children)

    def sel(text: str) -> list[str]:
        return [u.split(".")[-1] for u in dbt.select_models(project, text)]

    assert sel("mart") == ["mart"]
    assert sel("stg_*") == ["stg_a", "stg_b"]
    assert sel("tag:nightly") == ["mart", "stg_a"]
    assert sel("path:models/staging") == ["stg_a", "stg_b"]
    assert sel("+mart") == ["mart", "stg_a", "stg_b"]
    assert sel("stg_a+") == ["mart", "report", "stg_a"]
    assert sel("stg_a+1") == ["mart", "stg_a"]
    assert sel("tag:nightly,stg_*") == ["stg_a"]
    assert sel("report mart") == ["mart", "report"]
    with pytest.raises(ConfigError, match="not supported"):
        sel("state:modified")
    with pytest.raises(ConfigError, match="matched no models"):
        sel("nope")


def test_primary_key_sources() -> None:
    tests = {
        "test.p.unique_a": {
            "resource_type": "test",
            "attached_node": "model.p.a",
            "test_metadata": {"name": "unique", "kwargs": {"column_name": "id"}},
        },
        "test.p.combo_b": {
            "resource_type": "test",
            "depends_on": {"nodes": ["model.p.b"]},
            "test_metadata": {
                "name": "unique_combination_of_columns",
                "kwargs": {"combination_of_columns": ["loan_id", "as_of"]},
            },
        },
        "test.p.concat_c": {
            "resource_type": "test",
            "attached_node": "model.p.c",
            "test_metadata": {"name": "unique", "kwargs": {"column_name": "a || '-' || b"}},
        },
        "test.p.not_null": {
            "resource_type": "test",
            "attached_node": "model.p.d",
            "test_metadata": {"name": "not_null", "kwargs": {"column_name": "x"}},
        },
    }
    uniques = dbt.unique_columns({"nodes": tests})
    assert uniques == {
        "model.p.a": ["id"],
        "model.p.b": ["loan_id", "as_of"],
        "model.p.c": ["a", "b"],
    }
    assert dbt.primary_key(_model("a"), uniques) == ["id"]
    assert dbt.primary_key(_model("x", meta={"primary-key": "k"}), uniques) == ["k"]
    assert dbt.primary_key(
        _model("x", config={"meta": {"primary-key": ["k1", "k2"]}}), uniques
    ) == ["k1", "k2"]
    cols = {"id": {"meta": {"primary-key": True}}, "v": {}}
    assert dbt.primary_key(_model("x", columns=cols), uniques) == ["id"]
    cols = {"id": {"tags": ["primary-key"]}, "v": {}}
    assert dbt.primary_key(_model("x", columns=cols), uniques) == ["id"]
    assert dbt.primary_key(_model("d"), uniques) == []


def test_prod_relation_rules() -> None:
    settings = dbt.ProdSettings(
        database="analytics", schema="prod", custom_schema="prod_<custom_schema>"
    )
    node = _model("m")
    assert str(dbt.prod_relation(node, settings, None, None, None)) == "analytics.prod.m"
    custom = _model("m", config={"schema": "finance"})
    assert str(dbt.prod_relation(custom, settings, None, None, None)) == "analytics.prod_finance.m"
    with pytest.raises(ConfigError, match="custom schema"):
        dbt.prod_relation(custom, dbt.ProdSettings(schema="prod"), None, None, None)
    only_db = dbt.ProdSettings(database="analytics")
    assert str(dbt.prod_relation(node, only_db, None, None, None)) == "analytics.dev.m"
    assert str(dbt.prod_relation(node, settings, None, "other", "s")) == "other.s.m"


def test_render_resolves_env_var_only() -> None:
    env = {"PORT": "5439", "HOST": "h"}
    assert dbt.render(
        {"host": "{{ env_var('HOST') }}", "port": "{{ env_var('PORT') | int }}"}, env
    ) == {
        "host": "h",
        "port": 5439,
    }
    assert dbt.render("{{ env_var(\"MISSING\", 'dflt') }}", env) == "dflt"
    with pytest.raises(ConfigError, match="not set"):
        dbt.render("{{ env_var('MISSING') }}", env)
    with pytest.raises(ConfigError, match="unsupported Jinja"):
        dbt.render("{{ target.name }}", env)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            {
                "type": "postgres",
                "host": "h",
                "port": 5432,
                "user": "u",
                "pass": "p",
                "dbname": "d",
                "schema": "s",
                "threads": 4,
            },
            Connection(
                "postgres", {"host": "h", "port": 5432, "user": "u", "password": "p", "dbname": "d"}
            ),
        ),
        (
            {
                "type": "redshift",
                "host": "h",
                "port": 5439,
                "user": "u",
                "password": "p",
                "dbname": "dev",
            },
            Connection(
                "redshift",
                {"host": "h", "port": 5439, "user": "u", "password": "p", "database": "dev"},
            ),
        ),
        (
            {
                "type": "snowflake",
                "account": "a",
                "user": "u",
                "private_key_path": "/k",
                "role": "r",
                "database": "D",
                "warehouse": "W",
                "schema": "S",
            },
            Connection(
                "snowflake",
                {
                    "account": "a",
                    "user": "u",
                    "role": "r",
                    "warehouse": "W",
                    "database": "D",
                    "schema": "S",
                    "private_key_file": "/k",
                },
            ),
        ),
        (
            {
                "type": "bigquery",
                "method": "service-account",
                "project": "p",
                "keyfile": "/sa.json",
                "location": "EU",
                "dataset": "d",
            },
            Connection("bigquery", {"project": "p", "location": "EU", "keyfile": "/sa.json"}),
        ),
        ({"type": "duckdb", "path": "x.duckdb"}, Connection("duckdb", {"path": "x.duckdb"})),
    ],
)
def test_profile_outputs_become_connections(output: dict[str, Any], expected: Connection) -> None:
    assert dbt._connection(output, "p.t") == expected


@pytest.mark.parametrize(
    "output",
    [
        {"type": "spark"},
        {"type": "redshift", "method": "iam"},
        {"type": "bigquery", "method": "service-account-json"},
    ],
)
def test_unsupported_profiles(output: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match="not supported"):
        dbt._connection(output, "p.t")


def test_profiles_dir_search_order(tmp_path: Path) -> None:
    assert dbt.profiles_dir("/explicit", tmp_path, {"DBT_PROFILES_DIR": "/env"}) == Path(
        "/explicit"
    )
    assert dbt.profiles_dir(None, tmp_path, {"DBT_PROFILES_DIR": "/env"}) == Path("/env")
    (tmp_path / "profiles.yml").write_text("{}")
    assert dbt.profiles_dir(None, tmp_path, {}) == tmp_path
    assert dbt.profiles_dir(None, tmp_path / "nowhere", {}) == Path("~/.dbt").expanduser()


def test_target_from_environment(tmp_path: Path) -> None:
    (tmp_path / "profiles.yml").write_text(
        "p:\n  target: dev\n  outputs:\n    dev: {type: duckdb, path: dev.duckdb}\n    ci: {type: duckdb, path: ci.duckdb}\n"
    )
    conn, _ = dbt.connection_from_profile(tmp_path, "p", {"DBT_TARGET": "ci"})
    assert conn.config == {"path": "ci.duckdb"}
    with pytest.raises(ConfigError, match="no output"):
        dbt.connection_from_profile(tmp_path, "p", {"DBT_TARGET": "prod"})
