"""``reconkit data-diff``: data-diff's command line on top of reconkit's diff engine.

What is reproduced from data-diff (the archived ``datafold/data-diff``, last release 0.11):

* the four positional arguments ``DB1_URI TABLE1 DB2_URI TABLE2`` and the URI formats;
* ``--conf FILE --run NAME`` TOML files with ``[database.*]`` and ``[run.*]`` tables;
* which columns are compared: the key columns, ``--update-column`` and ``--columns``
  (with ``%`` wildcards), and nothing else unless asked;
* ``--where``, ``--min-age``, ``--max-age``, ``--limit``, ``--algorithm``,
  ``--bisection-factor`` and ``--bisection-threshold``;
* the output: ``- values`` / ``+ values`` lines, ``--json`` lines, and ``--stats``
  as text or JSON, with the same field names.

What differs, deliberately: errors exit non-zero (data-diff logged them and exited 0),
and ``--fail-on-diff`` exits 1 when differences are found.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, TextIO

from rich.console import Console

from reconkit.adapters import Adapter, create_adapter
from reconkit.compat.uri import Connection, from_dict, from_uri
from reconkit.diff.engine import TableSpec, diff_tables
from reconkit.diff.model import DiffOptions, DiffResult, RowDiff
from reconkit.errors import ConfigError
from reconkit.types import TableRef

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib  # type: ignore[import-not-found, no-redef, unused-ignore]

Strategy = Literal["auto", "bisect", "join"]
ALGORITHMS: dict[str, Strategy] = {"auto": "auto", "joindiff": "join", "hashdiff": "bisect"}

#: data-diff options that only made sense for its join-diff's materialised output or its
#: hosted service. Asking for one is an error, so nobody believes it happened.
UNSUPPORTED_OPTIONS = {
    "materialize_to_table": "--materialize-to-table",
    "materialize_all_rows": "--materialize-all-rows",
    "sample_exclusive_rows": "--sample-exclusive-rows",
    "cloud": "--cloud",
}


# -- time deltas (--min-age / --max-age), with data-diff's units ----------------------------

_UNITS = {
    "seconds": "seconds",
    "minutes": "minutes",
    "hours": "hours",
    "days": "days",
    "weeks": "weeks",
    "months": "months",
    "years": "years",
    "s": "seconds",
    "min": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
    "mon": "months",
    "y": "years",
}
_EXTRAPOLATED = {"months": 30, "years": 365}
_ATOM = re.compile(r"(\d+)([a-z]+)")


def parse_age(text: str) -> timedelta:
    """Parse ``5min``, ``1h30min``, ``2days``... Months are 30 days and years 365."""
    remaining, parts = text.strip(), {}
    if not remaining:
        raise ConfigError("no time difference specified")
    while remaining:
        match = _ATOM.match(remaining)
        if not match:
            raise ConfigError(f"cannot parse {remaining!r}: not a recognized time delta")
        count, unit = int(match.group(1)), match.group(2)
        if unit not in _UNITS:
            raise ConfigError(
                f"{unit!r} is not a recognized time unit; supported: {', '.join(sorted(_UNITS))}"
            )
        name = _UNITS[unit]
        if name in _EXTRAPOLATED:
            count, name = count * _EXTRAPOLATED[name], "days"
        if name in parts:
            raise ConfigError(f"time unit {name} specified more than once")
        parts[name] = count
        remaining = remaining[match.end() :]
    return timedelta(**parts)


# -- the request ------------------------------------------------------------------------------


@dataclass
class DataDiffArgs:
    """Everything ``reconkit data-diff`` was asked to do, after ``--conf`` is applied."""

    database1: Connection
    table1: str
    database2: Connection
    table2: str
    key_columns: list[str] = field(default_factory=lambda: ["id"])
    update_column: str | None = None
    columns: list[str] = field(default_factory=list)
    limit: int | None = None
    algorithm: str = "auto"
    bisection_factor: int | None = None
    bisection_threshold: int | None = None
    min_age: str | None = None
    max_age: str | None = None
    where: str | None = None
    stats: bool = False
    json_output: bool = False
    case_sensitive: bool = False
    assume_unique_key: bool = False
    fail_on_diff: bool = False


def connection(value: str | Connection | dict[str, Any], name: str = "") -> Connection:
    if isinstance(value, Connection):
        return value
    if isinstance(value, dict):
        return from_dict(value, name)
    return from_uri(value)


# -- --conf / --run ---------------------------------------------------------------------------

_ENV = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_RUN_KEYS = {
    "key_columns",
    "update_column",
    "columns",
    "limit",
    "algorithm",
    "bisection_factor",
    "bisection_threshold",
    "min_age",
    "max_age",
    "where",
    "stats",
    "json_output",
    "case_sensitive",
    "assume_unique_key",
    "threads",
    "verbose",
    "debug",
    "interactive",
    "no_tracking",
    "table_write_limit",
    *UNSUPPORTED_OPTIONS,
}


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            if match.group(1) not in os.environ:
                raise ConfigError(f"environment variable {match.group(1)} is not set")
            return os.environ[match.group(1)]

        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def load_conf(path: str | Path, run: str | None) -> dict[str, Any]:
    """Read a data-diff TOML file and return the chosen run's arguments.

    Follows data-diff's rules: ``[run.default]`` applies to every run, ``--run NAME``
    layers ``[run.NAME]`` on top, and each run names its two sides as ``1`` and ``2``,
    each with a ``database`` (a URI or a ``[database.*]`` name) and a ``table``.
    ``${VAR}`` is replaced from the environment.
    """
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    raw = _resolve_env(raw)
    databases = raw.pop("database", {})
    runs = raw.pop("run", {})
    if raw:
        raise ConfigError(f"{path}: unknown option(s): {sorted(raw)}")
    args: dict[str, Any] = dict(runs.get("default") or {})
    if run:
        if run not in runs:
            raise ConfigError(f"cannot find run {run!r} in {path} (runs: {sorted(runs)})")
        for key, value in runs[run].items():
            if key in ("1", "2") and isinstance(value, dict):
                args[key] = {**args.get(key, {}), **value}
            else:
                args[key] = value
    for index in ("1", "2"):
        side = args.pop(index, None)
        if side is None:
            continue
        if not isinstance(side, dict) or "database" not in side or "table" not in side:
            raise ConfigError(
                f"run {run or 'default'!r}: side {index} needs 'database' and 'table'"
            )
        unknown = set(side) - {"database", "table", "threads"}
        if unknown:
            raise ConfigError(f"unexpected attributes for side {index}: {sorted(unknown)}")
        db = side["database"]
        if "://" not in str(db):
            if db not in databases:
                raise ConfigError(f"database {db!r} not found; available: {sorted(databases)}")
            args[f"database{index}"] = connection(dict(databases[db]), str(db))
        else:
            args[f"database{index}"] = connection(str(db))
        args[f"table{index}"] = str(side["table"])
    unknown = set(args) - _RUN_KEYS - {"database1", "database2", "table1", "table2"}
    if unknown:
        raise ConfigError(f"run {run or 'default'!r}: unknown option(s): {sorted(unknown)}")
    return args


# -- running ----------------------------------------------------------------------------------


def _like(pattern: str, case_sensitive: bool) -> re.Pattern[str]:
    regex = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.compile(f"^{regex}$", 0 if case_sensitive else re.IGNORECASE)


def expand_columns(
    patterns: Sequence[str],
    left: Sequence[str],
    right: Sequence[str],
    *,
    case_sensitive: bool,
    left_label: str,
    right_label: str,
) -> list[str]:
    """Expand ``--columns`` patterns (``%`` matches anything) against the mutual columns.

    Returns left-table names in the left table's column order.
    """
    fold = (lambda s: s) if case_sensitive else str.lower
    right_names = {fold(c) for c in right}
    mutual = [c for c in left if fold(c) in right_names]
    chosen: set[str] = set()
    for pattern in patterns:
        rx = _like(pattern, case_sensitive)
        hits = {c for c in mutual if rx.match(c)}
        if not hits:
            missing = [
                label
                for label, cols in ((left_label, left), (right_label, right))
                if not any(rx.match(c) for c in cols)
            ]
            raise ConfigError(f"column {pattern!r} not found in: {', '.join(missing) or 'both'}")
        chosen |= hits
    return [c for c in mutual if c in chosen]


def _find(name: str, columns: Sequence[str], case_sensitive: bool, label: str) -> str:
    for column in columns:
        if column == name or (not case_sensitive and column.lower() == name.lower()):
            return column
    raise ConfigError(f"column {name!r} not found in {label}")


def _age_filter(adapter: Adapter, column: str, args: DataDiffArgs, now: datetime) -> list[str]:
    preds = []
    quoted = adapter.dialect.quote(column)
    for age, op in ((args.min_age, "<"), (args.max_age, ">")):
        if age:
            cutoff = (now - parse_age(age)).strftime("%Y-%m-%d %H:%M:%S.%f")
            preds.append(f"{quoted} {op} TIMESTAMP '{cutoff}'")
    return preds


def _where(*preds: str | None) -> str | None:
    parts = [p for p in preds if p]
    if not parts:
        return None
    return " AND ".join(f"({p})" for p in parts)


@dataclass
class DataDiffOutcome:
    result: DiffResult
    rows_a: int
    rows_b: int
    columns: list[str]
    label_a: str
    label_b: str

    @property
    def exclusive_a(self) -> int:
        return self.result.missing_in_right

    @property
    def exclusive_b(self) -> int:
        return self.result.missing_in_left

    @property
    def updated(self) -> int:
        return self.result.changed

    @property
    def unchanged(self) -> int:
        return self.rows_a - self.exclusive_a - self.updated

    @property
    def diff_percent(self) -> float:
        biggest = max(self.rows_a, self.rows_b)
        return 1 - self.unchanged / biggest if biggest else 0.0

    def extra_info(self) -> dict[str, Any]:
        r = self.result
        return {
            "strategy": r.strategy,
            "queries_A": r.left.queries,
            "queries_B": r.right.queries,
            "rows_downloaded": r.left.rows_fetched + r.right.rows_fetched,
            "duplicate_keys": len(r.duplicate_keys),
            "within_tolerance": r.within_tolerance,
            "duration_seconds": round(r.duration_seconds, 3),
        }


def _count(adapter: Adapter, table: str, where: str | None) -> int:
    sql = f"SELECT COUNT(*) FROM {adapter.dialect.table(TableRef.parse(table))}"
    if where:
        sql += f" WHERE {where}"
    return int(adapter.query(sql)[0][0])


def run_data_diff(
    args: DataDiffArgs,
    *,
    adapters: dict[Any, Adapter] | None = None,
    now: datetime | None = None,
) -> DataDiffOutcome:
    """Run the comparison data-diff would have run. ``adapters`` is for tests."""
    if (args.min_age or args.max_age) and not args.update_column:
        raise ConfigError("--min-age and --max-age need --update-column")
    if args.limit and args.stats:
        raise ConfigError("cannot specify a limit when using the -s/--stats switch")
    if args.algorithm not in ALGORITHMS:
        raise ConfigError(f"unknown algorithm {args.algorithm!r} (choose {', '.join(ALGORITHMS)})")
    now = now or datetime.now(tz=timezone.utc).replace(tzinfo=None)

    opened: dict[Any, Adapter] = {}
    pool = adapters if adapters is not None else opened

    def open_(conn: Connection, name: str) -> Adapter:
        ident = conn.identity()
        if ident not in pool:
            pool[ident] = create_adapter(conn.type, conn.config, name)
        return pool[ident]

    try:
        a = open_(args.database1, "A")
        b = open_(args.database2, "B")
        table_a = args.database1.qualify(args.table1)
        table_b = args.database2.qualify(args.table2)
        cols_a = [c.name for c in a.get_columns(TableRef.parse(table_a))]
        cols_b = [c.name for c in b.get_columns(TableRef.parse(table_b))]
        if not cols_a:
            raise ConfigError(f"table {table_a} not found or has no columns")
        if not cols_b:
            raise ConfigError(f"table {table_b} not found or has no columns")
        cs = args.case_sensitive
        keys = [_find(k, cols_a, cs, table_a) for k in args.key_columns]
        for k in args.key_columns:
            _find(k, cols_b, cs, table_b)
        update = _find(args.update_column, cols_a, cs, table_a) if args.update_column else None
        if update:
            _find(update, cols_b, cs, table_b)
        extras = expand_columns(
            args.columns, cols_a, cols_b, case_sensitive=cs, left_label=table_a, right_label=table_b
        )
        lowered = {k.lower() for k in keys} | ({update.lower()} if update else set())
        compared = ([update] if update else []) + [c for c in extras if c.lower() not in lowered]

        where_a = _where(args.where, *(_age_filter(a, update, args, now) if update else []))
        where_b = _where(args.where, *(_age_filter(b, update, args, now) if update else []))
        options = DiffOptions(strategy=ALGORITHMS[args.algorithm], limit=args.limit)
        if args.bisection_factor is not None:
            options = replace(options, bisection_factor=args.bisection_factor)
        if args.bisection_threshold is not None:
            options = replace(options, bisection_threshold=args.bisection_threshold)
        if options.strategy == "join" and a is not b:
            raise ConfigError("--algorithm joindiff needs both tables in the same database")
        result = diff_tables(
            TableSpec(a, table_a, where=where_a),
            TableSpec(b, table_b, where=where_b),
            keys,
            columns=compared,
            options=options,
        )
        rows_a, rows_b = result.left.row_count, result.right.row_count
        if args.stats:
            rows_a = rows_a if rows_a is not None else _count(a, table_a, where_a)
            rows_b = rows_b if rows_b is not None else _count(b, table_b, where_b)
        return DataDiffOutcome(
            result,
            rows_a or 0,
            rows_b or 0,
            [*keys, *compared],
            label_a=table_a,
            label_b=table_b,
        )
    finally:
        for adapter in opened.values():
            adapter.close()


# -- output -----------------------------------------------------------------------------------


def _values(row: dict[str, str | None] | None) -> list[str | None]:
    return list((row or {}).values())


def diff_lines(result: DiffResult) -> Iterator[tuple[str, list[str | None]]]:
    """data-diff's ``(sign, values)`` pairs: ``-`` is table A's row, ``+`` table B's."""
    for d in result.diffs:
        yield from _signed(d)


def _signed(d: RowDiff) -> Iterator[tuple[str, list[str | None]]]:
    if d.kind in ("missing_in_right", "changed"):
        yield "-", _values(d.left)
    if d.kind in ("missing_in_left", "changed"):
        yield "+", _values(d.right)


def _text(value: str | None) -> str:
    return "None" if value is None else value


def stats_text(outcome: DataDiffOutcome) -> str:
    lines = [
        f"{outcome.rows_a} rows in table A",
        f"{outcome.rows_b} rows in table B",
        f"{outcome.exclusive_a} rows exclusive to table A (not present in B)",
        f"{outcome.exclusive_b} rows exclusive to table B (not present in A)",
        f"{outcome.updated} rows updated",
        f"{outcome.unchanged} rows unchanged",
        f"{100 * outcome.diff_percent:.2f}% difference score",
        "",
        "Extra-Info:",
    ]
    lines += [f"  {k} = {v}" for k, v in sorted(outcome.extra_info().items())]
    return "\n".join(lines) + "\n"


def stats_dict(outcome: DataDiffOutcome) -> dict[str, Any]:
    return {
        "rows_A": outcome.rows_a,
        "rows_B": outcome.rows_b,
        "exclusive_A": outcome.exclusive_a,
        "exclusive_B": outcome.exclusive_b,
        "updated": outcome.updated,
        "unchanged": outcome.unchanged,
        "total": outcome.exclusive_a + outcome.exclusive_b + outcome.updated,
        "stats": outcome.extra_info(),
        "values": {},
    }


_COLORS = {"+": "green", "-": "red"}


def print_outcome(
    outcome: DataDiffOutcome,
    args: DataDiffArgs,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> None:
    console = Console(file=out or sys.stdout, highlight=False, soft_wrap=True)
    errors = Console(file=err or sys.stderr, highlight=False, soft_wrap=True)
    result = outcome.result
    for warning in result.warnings:
        errors.print(f"reconkit: warning: {warning}", markup=False)
    if result.duplicate_keys:
        errors.print(
            f"reconkit: warning: {len(result.duplicate_keys)} key value(s) occur more than once "
            "on one side; their rows are not compared",
            markup=False,
        )
    if args.stats:
        if args.json_output:
            console.print(json.dumps(stats_dict(outcome)), markup=False)
        else:
            console.print(stats_text(outcome), markup=False, end="")
        return
    for n, (sign, values) in enumerate(diff_lines(result)):
        if args.limit is not None and n >= args.limit:
            # data-diff's --limit counted output lines; a changed row is two of them.
            break
        if args.json_output:
            text = json.dumps([sign, values])
        else:
            text = f"{sign} {', '.join(_text(v) for v in values)}"
        console.print(text, markup=False, style=_COLORS[sign])
    if result.truncated:
        errors.print(f"reconkit: stopped after --limit {args.limit} differing rows", markup=False)
