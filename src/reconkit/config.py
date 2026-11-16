"""``reconkit.yml``: connections, named reconciliation checks and contract checks.

Secrets never live in the file. Any ``${VAR}`` or ``${VAR:-default}`` is replaced from the
environment, and keys that look like credentials are rejected unless their value is a
bare ``${VAR}`` reference.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from reconkit.diff.model import DiffOptions, Tolerance
from reconkit.errors import ConfigError

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_BARE_ENV = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_SECRET_KEY = re.compile(r"(password|passwd|secret|token|private_key|passphrase|credential)", re.I)
_URL_PASSWORD = re.compile(r"://[^/:@\s]+:([^@\s]+)@")
_TIMEZONE = re.compile(r"^[A-Za-z0-9_+\-/]+$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TableConfig(_Model):
    connection: str
    table: str
    where: str | None = None
    naive_timezone: str | None = None

    @field_validator("naive_timezone")
    @classmethod
    def _zone(cls, value: str | None) -> str | None:
        if value is not None and not _TIMEZONE.match(value):
            raise ValueError(f"invalid time zone name: {value!r}")
        return value


class ToleranceConfig(_Model):
    abs: Decimal | None = Field(default=None, ge=0)
    rel: Decimal | None = Field(default=None, ge=0)
    seconds: float | None = Field(default=None, ge=0)

    def to_tolerance(self) -> Tolerance:
        return Tolerance(self.abs, self.rel, self.seconds)


class OptionsConfig(_Model):
    """Every field is optional; unset fields fall back to ``defaults`` then built-ins."""

    strategy: Literal["auto", "bisect", "join"] | None = None
    bucketing: Literal["auto", "range", "hash"] | None = None
    bisection_factor: int | None = None
    bisection_threshold: int | None = None
    initial_buckets: int | None = None
    float_precision: int | None = None
    timestamp_precision: int | None = None
    trim_whitespace: bool | None = None
    empty_string_as_null: bool | None = None
    sample: float | None = None
    limit: int | None = None
    validate_unique_key: bool | None = None

    def apply(self, base: DiffOptions) -> DiffOptions:
        merged = DiffOptions(**{**base.__dict__, **self.model_dump(exclude_none=True)})
        merged.validate()
        return merged


class AuditConfig(_Model):
    connection: str
    table: str = "reconkit_audit"
    #: Cap on diff rows written per check run.
    max_rows: int = Field(default=10_000, ge=0)


class CheckConfig(_Model):
    description: str | None = None
    left: TableConfig
    right: TableConfig
    key: list[str]
    columns: list[str] | None = None
    ignore: list[str] = []
    mapping: dict[str, str] = {}
    tolerances: dict[str, ToleranceConfig] = {}
    precision: dict[str, int] = {}
    options: OptionsConfig = OptionsConfig()
    audit: AuditConfig | None = None

    @field_validator("key", mode="before")
    @classmethod
    def _key_list(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class ContractCheckConfig(_Model):
    connection: str
    #: Path to the contract file, relative to the config file.
    contract: str
    #: Overrides the contract's physicalName; only valid for single-table contracts.
    table: str | None = None


class Config(_Model):
    version: Literal[1] = 1
    connections: dict[str, dict[str, Any]] = {}
    defaults: OptionsConfig = OptionsConfig()
    audit: AuditConfig | None = None
    checks: dict[str, CheckConfig] = {}
    contracts: dict[str, ContractCheckConfig] = {}
    #: Directory of the config file; relative paths resolve against it.
    base_dir: Path = Field(default=Path("."), exclude=True)

    def options_for(self, check: CheckConfig) -> DiffOptions:
        return check.options.apply(self.defaults.apply(DiffOptions()))

    def connection(self, name: str) -> dict[str, Any]:
        if name not in self.connections:
            raise ConfigError(f"unknown connection {name!r} (defined: {sorted(self.connections)})")
        return self.connections[name]


def interpolate(value: Any, env: Mapping[str, str]) -> Any:
    """Replace ``${VAR}`` / ``${VAR:-default}`` in every string of a nested structure."""
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is not set")

        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: interpolate(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, env) for v in value]
    return value


def reject_plain_secrets(connections: Any) -> None:
    """Refuse literal credentials; they must come from the environment."""
    if not isinstance(connections, dict):
        return
    for conn_name, conn in connections.items():
        if not isinstance(conn, dict):
            continue
        for key, value in conn.items():
            if not isinstance(value, str):
                continue
            if _SECRET_KEY.search(str(key)) and not _BARE_ENV.match(value.strip()):
                raise ConfigError(
                    f"connections.{conn_name}.{key} must be an environment reference such as "
                    f"${{{str(key).upper()}}}; plain-text secrets are not accepted"
                )
            for match in _URL_PASSWORD.finditer(value):
                if not _BARE_ENV.match(match.group(1)):
                    raise ConfigError(
                        f"connections.{conn_name}.{key} embeds a plain-text password; "
                        "use ${VAR} for the password part"
                    )


def format_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)


def parse_config(
    raw: Any, *, env: Mapping[str, str] | None = None, base_dir: Path | None = None
) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a YAML mapping")
    reject_plain_secrets(raw.get("connections"))
    data = interpolate(raw, os.environ if env is None else env)
    for name, conn in (data.get("connections") or {}).items():
        if not isinstance(conn, dict) or "type" not in conn:
            raise ConfigError(f"connections.{name} needs a 'type'")
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid config:\n{format_validation_error(exc)}") from exc
    config.base_dir = base_dir or Path(".")
    for name, check in config.checks.items():
        for side in (check.left, check.right):
            config.connection(side.connection)
        if check.audit:
            config.connection(check.audit.connection)
        config.options_for(check)
        del name
    for contract in config.contracts.values():
        config.connection(contract.connection)
    if config.audit:
        config.connection(config.audit.connection)
    return config


def load_config(path: str | Path, *, env: Mapping[str, str] | None = None) -> Config:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    return parse_config(raw, env=env, base_dir=path.parent)
