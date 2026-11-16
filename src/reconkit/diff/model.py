"""Result and option types for table diffs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Literal

from reconkit.errors import ConfigError

Key = tuple[Any, ...]
DiffKind = Literal["missing_in_left", "missing_in_right", "changed"]


@dataclass(frozen=True)
class Tolerance:
    """Differences inside the tolerance are not reported.

    ``abs`` and ``rel`` apply to numeric columns (either one passing is enough);
    ``seconds`` applies to date and timestamp columns.
    """

    abs: Decimal | None = None
    rel: Decimal | None = None
    seconds: float | None = None


@dataclass
class DiffOptions:
    #: ``auto`` picks ``join`` when both tables share one connection, else ``bisect``.
    strategy: Literal["auto", "bisect", "join"] = "auto"
    #: ``auto`` picks ``range`` when the first key column is an integer, else ``hash``.
    bucketing: Literal["auto", "range", "hash"] = "auto"
    #: How many children each mismatching bucket is split into.
    bisection_factor: int = 32
    #: Buckets with at most this many rows are downloaded instead of split further.
    bisection_threshold: int = 64
    #: Upper bound on the number of buckets in the first, full-table checksum pass.
    #: None sizes it from the row count (``RangePlan.for_rows``, ``auto_hash_buckets``).
    initial_buckets: int | None = None
    #: Decimal places floats (and unconstrained numerics) are rounded to.
    float_precision: int = 6
    #: Fractional-second digits compared; further digits are truncated.
    timestamp_precision: int = 6
    trim_whitespace: bool = False
    empty_string_as_null: bool = False
    #: Compare a deterministic key-hash sample, e.g. 0.01 for about 1% of keys.
    sample: float | None = None
    #: Stop after this many differing rows.
    limit: int | None = None
    #: Fail up front if the key is not unique on either side (one extra query per side).
    validate_unique_key: bool = False
    #: Maximum number of key ranges addressed by one statement (hash bucketing packs 64
    #: times as many, see ``reconkit.diff.bisect.HASH_BATCH_FACTOR``).
    max_buckets_per_query: int = 256

    def validate(self) -> None:
        if self.bisection_factor < 2:
            raise ConfigError("bisection_factor must be >= 2")
        if self.bisection_threshold < 1:
            raise ConfigError("bisection_threshold must be >= 1")
        if self.initial_buckets is not None and self.initial_buckets < 1:
            raise ConfigError("initial_buckets must be >= 1")
        if not 0 <= self.timestamp_precision <= 6:
            raise ConfigError("timestamp_precision must be between 0 and 6")
        if not 0 <= self.float_precision <= 18:
            raise ConfigError("float_precision must be between 0 and 18")
        if self.sample is not None and not 0 < self.sample <= 1:
            raise ConfigError("sample must be in (0, 1]")
        if self.limit is not None and self.limit < 1:
            raise ConfigError("limit must be >= 1")
        if self.max_buckets_per_query < 1:
            raise ConfigError("max_buckets_per_query must be >= 1")


@dataclass(frozen=True)
class ColumnDiff:
    column: str
    left: str | None
    right: str | None


@dataclass(frozen=True)
class RowDiff:
    kind: DiffKind
    key: Key
    #: Canonical values of the row on each side (None where the row is missing).
    left: dict[str, str | None] | None = None
    right: dict[str, str | None] | None = None
    #: For ``changed`` rows, the columns that differ beyond tolerance.
    columns: tuple[ColumnDiff, ...] = ()


@dataclass
class SideStats:
    table: str = ""
    connection: str = ""
    #: Rows visible through the filter (and sample), when the strategy learns it.
    row_count: int | None = None
    queries: int = 0
    #: Every row the database returned, checksum rows included.
    rows_returned: int = 0
    #: Table rows downloaded for local comparison.
    rows_fetched: int = 0


@dataclass
class DiffResult:
    key_columns: list[str]
    compared_columns: list[str]
    strategy: str
    left: SideStats
    right: SideStats
    diffs: list[RowDiff] = field(default_factory=list)
    #: Keys seen more than once on one side inside a compared bucket.
    duplicate_keys: list[Key] = field(default_factory=list)
    #: Rows whose checksums differed but which are equal once tolerances are applied.
    within_tolerance: int = 0
    warnings: list[str] = field(default_factory=list)
    sample_rate: float | None = None
    truncated: bool = False
    levels: int = 0
    duration_seconds: float = 0.0

    def count(self, kind: DiffKind) -> int:
        return sum(1 for d in self.diffs if d.kind == kind)

    @property
    def missing_in_left(self) -> int:
        return self.count("missing_in_left")

    @property
    def missing_in_right(self) -> int:
        return self.count("missing_in_right")

    @property
    def changed(self) -> int:
        return self.count("changed")

    @property
    def is_match(self) -> bool:
        return not self.diffs and not self.duplicate_keys

    def column_change_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.diffs:
            for c in d.columns:
                counts[c.column] = counts.get(c.column, 0) + 1
        return counts

    def to_dict(self, max_rows: int | None = None) -> dict[str, Any]:
        rows = self.diffs if max_rows is None else self.diffs[:max_rows]
        return {
            "match": self.is_match,
            "strategy": self.strategy,
            "key_columns": self.key_columns,
            "compared_columns": self.compared_columns,
            "summary": {
                "missing_in_left": self.missing_in_left,
                "missing_in_right": self.missing_in_right,
                "changed": self.changed,
                "changed_by_column": self.column_change_counts(),
                "within_tolerance": self.within_tolerance,
                "duplicate_keys": len(self.duplicate_keys),
            },
            "left": asdict(self.left),
            "right": asdict(self.right),
            "sample_rate": self.sample_rate,
            "truncated": self.truncated,
            "levels": self.levels,
            "duration_seconds": round(self.duration_seconds, 4),
            "warnings": self.warnings,
            "rows_listed": len(rows),
            "rows": [
                {
                    "kind": d.kind,
                    "key": dict(zip(self.key_columns, d.key, strict=True)),
                    "left": d.left,
                    "right": d.right,
                    "columns": [asdict(c) for c in d.columns],
                }
                for d in rows
            ],
        }
