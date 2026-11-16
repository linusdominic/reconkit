from __future__ import annotations

from decimal import Decimal

import pytest

from reconkit.diff.compare import ColumnRule, compare_rows, parse_timestamp, sort_key, values_equal
from reconkit.diff.model import DiffOptions, Tolerance
from reconkit.diff.resolve import resolve_canon, resolve_columns
from reconkit.errors import ConfigError, DiffError
from reconkit.types import Canon, ColumnInfo, ColumnType, Kind

DEC = Canon(Kind.DECIMAL, scale=2)
TS = Canon(Kind.TIMESTAMP)


def rule(canon: Canon, **tol: object) -> ColumnRule:
    return ColumnRule("c", canon, Tolerance(**tol) if tol else None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("left", "right", "r", "equal"),
    [
        ("1.00", "1.00", rule(DEC), True),
        ("1.00", "1.01", rule(DEC), False),
        ("1.00", "1.01", rule(DEC, abs=Decimal("0.01")), True),
        ("1.00", "1.02", rule(DEC, abs=Decimal("0.01")), False),
        ("1000.00", "1000.90", rule(DEC, rel=Decimal("0.001")), True),
        ("1000.00", "1002.00", rule(DEC, rel=Decimal("0.001")), False),
        ("-5.00", "5.00", rule(DEC, rel=Decimal("0.5")), False),
        (None, None, rule(DEC, abs=Decimal("1")), True),
        (None, "0.00", rule(DEC, abs=Decimal("1")), False),
        ("2024-01-01 00:00:00.000000", "2024-01-01 00:00:01.000000", rule(TS, seconds=1), True),
        ("2024-01-01 00:00:00.000000", "2024-01-01 00:00:01.000001", rule(TS, seconds=1), False),
        ("2024-01-01", "2024-01-02", rule(Canon(Kind.DATE), seconds=86400), True),
        ("abc", "abd", ColumnRule("c", Canon(Kind.TEXT), Tolerance(abs=Decimal("9"))), False),
        ("not-a-number", "1.00", rule(DEC, abs=Decimal("1")), False),
    ],
)
def test_values_equal(left: str | None, right: str | None, r: ColumnRule, equal: bool) -> None:
    assert values_equal(left, right, r) is equal
    assert values_equal(right, left, r) is equal


def test_parse_timestamp_handles_truncated_fractions() -> None:
    assert parse_timestamp("2024-05-06 07:08:09.5").microsecond == 500_000
    assert parse_timestamp("2024-05-06 07:08:09").second == 9
    assert parse_timestamp("2024-05-06").day == 6


def test_sort_key_orders_numeric_keys_numerically() -> None:
    keys = [("10",), ("9",), (None,), ("-3",), ("abc",)]
    assert sorted(keys, key=sort_key) == [(None,), ("-3",), ("9",), ("10",), ("abc",)]


def test_compare_rows_classifies_and_reports_columns() -> None:
    rules = [
        ColumnRule("amount", DEC, Tolerance(abs=Decimal("0.01"))),
        ColumnRule("name", Canon(Kind.TEXT)),
    ]
    left = [("1", "5.00", "a"), ("2", "5.00", "b"), ("3", "5.00", None), ("5", "1.00", "x")]
    right = [("2", "5.01", "b"), ("3", "9.00", "c"), ("4", "5.00", "d"), ("5", "1.00", "x")]
    out = compare_rows(left, right, ["id"], rules)
    assert [(d.kind, d.key) for d in out.diffs] == [
        ("missing_in_right", ("1",)),
        ("changed", ("3",)),
        ("missing_in_left", ("4",)),
    ]
    changed = out.diffs[1]
    assert [(c.column, c.left, c.right) for c in changed.columns] == [
        ("amount", "5.00", "9.00"),
        ("name", None, "c"),
    ]
    assert changed.left == {"id": "3", "amount": "5.00", "name": None}
    assert out.within_tolerance == 1  # key 2 differs only by 0.01
    assert out.diffs[0].left == {"id": "1", "amount": "5.00", "name": "a"}
    assert out.diffs[2].right == {"id": "4", "amount": "5.00", "name": "d"}


def test_compare_rows_flags_duplicate_keys() -> None:
    out = compare_rows(
        [("1", "a"), ("1", "b")], [("1", "a")], ["id"], [ColumnRule("v", Canon(Kind.TEXT))]
    )
    assert out.duplicate_keys == [("1",)]


def T(kind: Kind, **kw: object) -> ColumnType:
    return ColumnType(kind, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (T(Kind.INTEGER), T(Kind.INTEGER), Canon(Kind.INTEGER)),
        (T(Kind.BOOLEAN), T(Kind.BOOLEAN), Canon(Kind.BOOLEAN)),
        (T(Kind.BOOLEAN), T(Kind.INTEGER), Canon(Kind.INTEGER)),
        (T(Kind.DECIMAL, scale=4), T(Kind.DECIMAL, scale=2), Canon(Kind.DECIMAL, scale=2)),
        (T(Kind.INTEGER), T(Kind.DECIMAL, scale=2), Canon(Kind.DECIMAL, scale=2)),
        (T(Kind.DECIMAL, scale=4), T(Kind.FLOAT), Canon(Kind.DECIMAL, scale=4)),
        (T(Kind.DECIMAL, scale=10), T(Kind.FLOAT), Canon(Kind.DECIMAL, scale=6)),
        (T(Kind.DECIMAL, scale=None), T(Kind.DECIMAL, scale=None), Canon(Kind.DECIMAL, scale=6)),
        (T(Kind.FLOAT), T(Kind.INTEGER), Canon(Kind.DECIMAL, scale=6)),
        (T(Kind.DATE), T(Kind.DATE), Canon(Kind.DATE)),
        (T(Kind.DATE), T(Kind.TIMESTAMP), Canon(Kind.TIMESTAMP, shift_naive=False)),
        (T(Kind.TIMESTAMP, tz_aware=True), T(Kind.TIMESTAMP), Canon(Kind.TIMESTAMP)),
        (T(Kind.UUID), T(Kind.TEXT), Canon(Kind.UUID)),
        (T(Kind.TEXT), T(Kind.TEXT), Canon(Kind.TEXT)),
        (T(Kind.OTHER), T(Kind.TEXT), Canon(Kind.TEXT)),
        (T(Kind.INTEGER), T(Kind.TEXT), Canon(Kind.TEXT)),
    ],
)
def test_resolve_canon(left: ColumnType, right: ColumnType, expected: Canon) -> None:
    assert resolve_canon(left, right, DiffOptions()) == expected
    assert resolve_canon(right, left, DiffOptions()) == expected


def test_resolve_canon_precision_override_and_text_options() -> None:
    opts = DiffOptions(trim_whitespace=True, empty_string_as_null=True, timestamp_precision=3)
    assert resolve_canon(T(Kind.FLOAT), T(Kind.FLOAT), opts, precision=2).scale == 2
    assert resolve_canon(T(Kind.TIMESTAMP), T(Kind.TIMESTAMP), opts).ts_precision == 3
    assert resolve_canon(T(Kind.TIMESTAMP), T(Kind.TIMESTAMP), opts, precision=0).ts_precision == 0
    assert resolve_canon(T(Kind.TEXT), T(Kind.TEXT), opts) == Canon(
        Kind.TEXT, trim=True, empty_as_null=True
    )


def cols(**kinds: Kind) -> list[ColumnInfo]:
    return [
        ColumnInfo(name, ColumnType(kind, scale=2, native=kind.value))
        for name, kind in kinds.items()
    ]


def test_resolve_columns_mapping_ignore_and_warnings() -> None:
    left = cols(
        ID=Kind.INTEGER, status=Kind.TEXT, amount=Kind.DECIMAL, only_left=Kind.TEXT, skip=Kind.TEXT
    )
    right = cols(
        id=Kind.INTEGER, loan_status=Kind.TEXT, amount=Kind.DECIMAL, skip=Kind.TEXT, extra=Kind.TEXT
    )
    keys, values, warnings = resolve_columns(
        left, right, ["id"], mapping={"STATUS": "loan_status"}, ignore=["Skip"],
        tolerances={"amount": Tolerance(abs=Decimal("1"))}, options=DiffOptions(),
    )  # fmt: skip
    assert [(p.name, p.left.name, p.right.name) for p in keys] == [("ID", "ID", "id")]
    assert [(p.name, p.right.name) for p in values] == [
        ("status", "loan_status"),
        ("amount", "amount"),
    ]
    assert values[1].tolerance == Tolerance(abs=Decimal("1"))
    assert any("only in the right table" in w and "extra" in w for w in warnings)
    assert any("'only_left' only in the left table" in w for w in warnings)


def test_resolve_columns_explicit_list_and_errors() -> None:
    left = cols(id=Kind.INTEGER, a=Kind.TEXT, b=Kind.TEXT)
    right = cols(id=Kind.INTEGER, a=Kind.TEXT)
    _, values, _ = resolve_columns(left, right, ["id"], columns=["a", "id"], options=DiffOptions())
    assert [p.name for p in values] == ["a"]
    with pytest.raises(DiffError, match="'b' not found in the right"):
        resolve_columns(left, right, ["id"], columns=["b"], options=DiffOptions())
    with pytest.raises(DiffError, match="'nope' not found in the left"):
        resolve_columns(left, right, ["nope"], options=DiffOptions())
    with pytest.raises(DiffError, match="at least one key"):
        resolve_columns(left, right, [], options=DiffOptions())
    with pytest.raises(DiffError, match="tolerances refers to columns not in the left"):
        resolve_columns(left, right, ["id"], tolerances={"zzz": Tolerance()}, options=DiffOptions())
    with pytest.raises(DiffError, match="does not apply to a text column"):
        resolve_columns(
            left, right, ["id"], tolerances={"a": Tolerance(abs=Decimal(1))}, options=DiffOptions()
        )


def test_resolve_columns_warns_on_float_key_and_incomparable_types() -> None:
    left = cols(k=Kind.FLOAT, v=Kind.OTHER)
    right = cols(k=Kind.FLOAT, v=Kind.TEXT)
    _, _, warnings = resolve_columns(left, right, ["k"], options=DiffOptions())
    assert any("float" in w for w in warnings)
    assert any("no shared canonical form" in w for w in warnings)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bisection_factor": 1}, {"bisection_threshold": 0}, {"initial_buckets": 0},
        {"timestamp_precision": 7}, {"float_precision": -1}, {"sample": 0}, {"sample": 1.5},
        {"limit": 0}, {"max_buckets_per_query": 0},
    ],
)  # fmt: skip
def test_options_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        DiffOptions(**kwargs).validate()  # type: ignore[arg-type]
