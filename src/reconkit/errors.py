"""Exception hierarchy. The CLI maps these to exit codes (see reconkit.cli)."""

from __future__ import annotations


class ReconkitError(Exception):
    """Base class for all errors raised deliberately by reconkit."""


class ConfigError(ReconkitError):
    """The configuration, contract file or command line is invalid."""


class AdapterError(ReconkitError):
    """A database driver is missing, a connection failed or a query failed."""


class DiffError(ReconkitError):
    """The two tables cannot be compared as requested (bad key, no common columns, ...)."""
