"""Adapter registry.

Third-party adapters register a factory with :func:`register`; see docs/adapters.md.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

from reconkit.adapters.base import Adapter, DbApiAdapter, Dialect, QueryStats
from reconkit.errors import ConfigError

AdapterFactory = Callable[[dict[str, Any], str], Adapter]

_BUILTIN = {
    "postgres": "reconkit.adapters.postgres",
    "postgresql": "reconkit.adapters.postgres",
    "duckdb": "reconkit.adapters.duckdb",
    "snowflake": "reconkit.adapters.snowflake",
    "bigquery": "reconkit.adapters.bigquery",
    "redshift": "reconkit.adapters.redshift",
}
_REGISTRY: dict[str, AdapterFactory] = {}


def register(type_name: str, factory: AdapterFactory) -> None:
    """Register an adapter factory for ``type: <type_name>`` connections."""
    _REGISTRY[type_name.lower()] = factory


def create_adapter(type_name: str, config: dict[str, Any], name: str = "") -> Adapter:
    key = type_name.lower()
    if key in _REGISTRY:
        return _REGISTRY[key](config, name or key)
    if key in _BUILTIN:
        factory: AdapterFactory = import_module(_BUILTIN[key]).connect
        return factory(config, name or key)
    known = ", ".join(sorted(set(_BUILTIN) | set(_REGISTRY)))
    raise ConfigError(f"unknown connection type {type_name!r} (known: {known})")


__all__ = ["Adapter", "DbApiAdapter", "Dialect", "QueryStats", "create_adapter", "register"]
