"""Engine-neutral column types and table references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from reconkit.errors import ConfigError


class Kind(str, Enum):
    """Coarse type families. Normalisation rules are defined per family."""

    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    TEXT = "text"
    UUID = "uuid"
    DATE = "date"
    TIMESTAMP = "timestamp"
    OTHER = "other"


@dataclass(frozen=True)
class ColumnType:
    kind: Kind
    #: Decimal places for DECIMAL columns. None means unconstrained (e.g. Postgres ``numeric``).
    scale: int | None = None
    #: True for timestamp types that store an absolute instant (``timestamptz``).
    tz_aware: bool = False
    #: The engine's own type name, kept for messages and contract checks.
    native: str = ""


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: ColumnType
    nullable: bool = True


_PART = re.compile(r'^(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+"|`[^`]+`)$')
_SPLIT = re.compile(r'("(?:[^"]|"")+"|`[^`]+`|[^.]+)')


@dataclass(frozen=True)
class TableRef:
    """A possibly qualified table name, e.g. ``schema.table`` or ``db.schema."Mixed Case"``.

    Parts are validated when parsed and emitted verbatim, so an unquoted part keeps the
    engine's own case-folding rules and a quoted part stays quoted.
    """

    parts: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> TableRef:
        parts = tuple(_SPLIT.findall(text.strip()))
        if not parts or ".".join(parts) != text.strip() or len(parts) > 3:
            raise ConfigError(f"invalid table reference: {text!r}")
        for part in parts:
            if not _PART.match(part):
                raise ConfigError(f"invalid identifier {part!r} in table reference {text!r}")
        return cls(parts)

    @property
    def name(self) -> str:
        return self.parts[-1]

    @property
    def schema(self) -> str | None:
        return self.parts[-2] if len(self.parts) >= 2 else None

    @property
    def catalog(self) -> str | None:
        return self.parts[-3] if len(self.parts) >= 3 else None

    def __str__(self) -> str:
        return ".".join(self.parts)


def is_quoted(part: str) -> bool:
    return part[:1] in ('"', "`")


def unquote(part: str) -> str:
    if part.startswith('"'):
        return part[1:-1].replace('""', '"')
    if part.startswith("`"):
        return part[1:-1]
    return part


@dataclass(frozen=True)
class Canon:
    """The canonical text form two columns are both normalised to before comparison.

    One ``Canon`` is resolved per compared column pair (see ``reconkit.diff.resolve``) and
    handed to each side's dialect, which renders SQL producing exactly that text form.
    """

    kind: Kind
    #: Decimal places, for DECIMAL.
    scale: int = 0
    #: Fractional-second digits kept, for TIMESTAMP (0-6). Extra digits are truncated.
    ts_precision: int = 6
    #: Whether a side's ``naive_timezone`` applies, for TIMESTAMP. False when one side is a
    #: DATE: a date has no zone, so the other side's midnight must not be shifted.
    shift_naive: bool = True
    #: Strip trailing whitespace, for TEXT.
    trim: bool = False
    #: Treat '' as NULL, for TEXT.
    empty_as_null: bool = False
