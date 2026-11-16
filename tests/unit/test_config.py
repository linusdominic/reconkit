from __future__ import annotations

from pathlib import Path

import pytest

from reconkit.config import interpolate, load_config, parse_config
from reconkit.errors import ConfigError

BASE = {
    "connections": {
        "oltp": {"type": "postgres", "host": "${PGHOST:-localhost}", "password": "${PGPASSWORD}"},
        "wh": {"type": "duckdb", "path": "wh.duckdb"},
    },
    "defaults": {"bisection_factor": 16, "float_precision": 4},
    "checks": {
        "loans": {
            "left": {"connection": "oltp", "table": "public.loans"},
            "right": {"connection": "wh", "table": "loans", "naive_timezone": "Asia/Singapore"},
            "key": "loan_id",
            "tolerances": {"rate": {"abs": "0.0001"}},
            "options": {"bisection_factor": 8, "sample": 0.5},
        }
    },
}


def test_parse_interpolates_env_and_merges_options() -> None:
    config = parse_config(BASE, env={"PGPASSWORD": "s3cret"})
    assert config.connections["oltp"]["password"] == "s3cret"
    assert config.connections["oltp"]["host"] == "localhost"
    check = config.checks["loans"]
    assert check.key == ["loan_id"]
    opts = config.options_for(check)
    assert (opts.bisection_factor, opts.float_precision, opts.sample) == (8, 4, 0.5)
    assert opts.bisection_threshold == 64  # built-in default survives
    assert str(check.tolerances["rate"].to_tolerance().abs) == "0.0001"


def test_interpolate_default_and_missing() -> None:
    assert interpolate({"a": ["${X:-d}", 1]}, {}) == {"a": ["d", 1]}
    assert interpolate("${X:-d}", {"X": "set"}) == "set"
    assert interpolate("${X:-}", {}) == ""
    with pytest.raises(ConfigError, match="environment variable PGPASSWORD is not set"):
        parse_config(BASE, env={})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("password", "hunter2"),
        ("password", "${PGPASSWORD:-hunter2}"),
        ("private_key", "-----BEGIN"),
        ("api_token", "abc"),
        ("url", "postgresql://app:hunter2@db/prod"),
    ],
)
def test_plain_text_secrets_are_rejected(key: str, value: str) -> None:
    raw = {"connections": {"c": {"type": "postgres", key: value}}}
    with pytest.raises(ConfigError, match="plain-text"):
        parse_config(raw, env={})


def test_url_with_env_password_is_accepted() -> None:
    raw = {"connections": {"c": {"type": "postgres", "url": "postgresql://app:${PW}@db/prod"}}}
    config = parse_config(raw, env={"PW": "x"})
    assert config.connections["c"]["url"] == "postgresql://app:x@db/prod"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda c: c["checks"]["loans"]["left"].update(connection="nope"),
            "unknown connection 'nope'",
        ),
        (lambda c: c["checks"]["loans"].update(kee="x"), "checks.loans.kee"),
        (lambda c: c["checks"]["loans"].pop("key"), "checks.loans.key"),
        (lambda c: c["checks"]["loans"]["options"].update(bisection_factor=1), "bisection_factor"),
        (lambda c: c["checks"]["loans"]["options"].update(strategy="magic"), "strategy"),
        (lambda c: c["checks"]["loans"]["right"].update(naive_timezone="x'; --"), "time zone"),
        (
            lambda c: c["checks"]["loans"]["tolerances"]["rate"].update(abs=-1),
            "tolerances.rate.abs",
        ),
        (lambda c: c["connections"]["wh"].pop("type"), "needs a 'type'"),
        (lambda c: c.update(audit={"connection": "missing"}), "unknown connection 'missing'"),
        (lambda c: c.update(contracts={"x": {"connection": "zz", "contract": "c.yml"}}), "'zz'"),
        (lambda c: c.update(version=2), "version"),
    ],
)
def test_invalid_configs(mutate: object, message: str) -> None:
    import copy

    raw = copy.deepcopy(BASE)
    mutate(raw)  # type: ignore[operator]
    with pytest.raises(ConfigError, match=message):
        parse_config(raw, env={"PGPASSWORD": "x"})


def test_load_config_from_file(tmp_path: Path) -> None:
    path = tmp_path / "reconkit.yml"
    path.write_text("connections:\n  wh: {type: duckdb}\n")
    config = load_config(path, env={})
    assert config.base_dir == tmp_path
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yml")
    path.write_text("a: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)
    path.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError, match="must be a YAML mapping"):
        load_config(path)
