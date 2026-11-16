"""Property-based tests: reconkit's answer must equal a brute-force oracle's.

Each example builds two random tables in separate in-memory DuckDB databases, with
bisection parameters small enough to force deep recursion, and compares the reported
diffs with a plain Python dictionary comparison.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from reconkit import DiffOptions, TableSpec, diff_tables
from reconkit.adapters import DbApiAdapter
from tests.conftest import load, make_duck

TEXT = st.one_of(
    st.none(),
    st.text(alphabet=st.sampled_from(list("abN|V '\"é☃ ")), max_size=4),
)
AMOUNT = st.one_of(st.none(), st.decimals(min_value=-999, max_value=999, places=2))
VALUES = st.tuples(
    AMOUNT, TEXT, st.one_of(st.none(), st.booleans()), st.one_of(st.none(), st.integers(-3, 3))
)
INT_KEYS = st.integers(-40, 400)
TEXT_KEYS = st.text(alphabet="abcXYZ-|é", min_size=0, max_size=3)
OPTIONS = st.fixed_dictionaries(
    {
        "bisection_factor": st.sampled_from([2, 3, 8, 32]),
        "bisection_threshold": st.sampled_from([1, 2, 10, 64]),
        "initial_buckets": st.sampled_from([None, 1, 2, 7, 64]),
        "max_buckets_per_query": st.sampled_from([1, 3, 256]),
    }
)


@st.composite
def table_pair(
    draw: st.DrawFn, keys: st.SearchStrategy[Any]
) -> tuple[dict[Any, Any], dict[Any, Any]]:
    """Two tables that mostly agree, so that every kind of difference shows up."""
    left = draw(st.dictionaries(keys, VALUES, max_size=60))
    right = dict(left)
    for key in draw(st.lists(st.sampled_from(sorted(left, key=repr)), max_size=8)) if left else []:
        if draw(st.booleans()):
            right.pop(key, None)
        else:
            right[key] = draw(VALUES)
    right.update(draw(st.dictionaries(keys, VALUES, max_size=8)))
    return left, right


def canon(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return str(value)


def oracle(left: dict[Any, Any], right: dict[Any, Any], key_width: int) -> set[tuple[Any, ...]]:
    def key_of(k: Any) -> tuple[str | None, ...]:
        return tuple(canon(p) for p in (k if key_width > 1 else (k,)))

    names = ["amount", "name", "flag", "n"]
    out: set[tuple[Any, ...]] = set()
    for k in left.keys() | right.keys():
        if k not in left:
            out.add(("missing_in_left", key_of(k), ()))
        elif k not in right:
            out.add(("missing_in_right", key_of(k), ()))
        else:
            cols = tuple(
                (n, canon(a), canon(b))
                for n, a, b in zip(names, left[k], right[k], strict=True)
                if canon(a) != canon(b)
            )
            if cols:
                out.add(("changed", key_of(k), cols))
    return out


def reported(result: Any) -> set[tuple[Any, ...]]:
    return {
        (d.kind, d.key, tuple((c.column, c.left, c.right) for c in d.columns)) for d in result.diffs
    }


def rows(table: dict[Any, Any], key_width: int) -> list[tuple[Any, ...]]:
    return [(*(k if key_width > 1 else (k,)), *v) for k, v in table.items()]


def run(
    left: dict[Any, Any], right: dict[Any, Any], key_ddl: str, key: list[str], **opts: Any
) -> None:
    a, b = make_duck("left"), make_duck("right")
    try:
        ddl = f"{key_ddl}, amount DECIMAL(10,2), name VARCHAR, flag BOOLEAN, n INTEGER"
        load(a, "t", ddl, rows(left, len(key)))
        load(b, "t", ddl, rows(right, len(key)))
        expected = oracle(left, right, len(key))
        result = diff_tables(TableSpec(a, "t"), TableSpec(b, "t"), key, options=DiffOptions(**opts))
        assert reported(result) == expected
        assert not result.duplicate_keys
        assert result.is_match == (not expected)
        assert (result.left.row_count, result.right.row_count) == (len(left), len(right))
        # The join strategy on a single connection must agree with bisection.
        load(a, "u", ddl, rows(right, len(key)))
        joined = diff_tables(TableSpec(a, "t"), TableSpec(a, "u"), key)
        assert joined.strategy == "join"
        assert reported(joined) == expected
    finally:
        a.close()
        b.close()


COMMON = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@COMMON
@given(pair=table_pair(INT_KEYS), opts=OPTIONS)
def test_range_bisection_matches_oracle(
    pair: tuple[dict[Any, Any], dict[Any, Any]], opts: dict[str, int]
) -> None:
    run(*pair, "id INTEGER", ["id"], bucketing="range", **opts)


@COMMON
@given(pair=table_pair(INT_KEYS), opts=OPTIONS)
def test_hash_bisection_matches_oracle(
    pair: tuple[dict[Any, Any], dict[Any, Any]], opts: dict[str, int]
) -> None:
    run(*pair, "id INTEGER", ["id"], bucketing="hash", **opts)


@COMMON
@given(pair=table_pair(st.tuples(TEXT_KEYS, st.integers(0, 3))), opts=OPTIONS)
def test_composite_text_key_matches_oracle(
    pair: tuple[dict[Any, Any], dict[Any, Any]], opts: dict[str, int]
) -> None:
    run(*pair, "code VARCHAR, seq INTEGER", ["code", "seq"], **opts)


@COMMON
@given(pair=table_pair(st.tuples(st.integers(-5, 20), TEXT_KEYS)), opts=OPTIONS)
def test_composite_integer_first_key_matches_oracle(
    pair: tuple[dict[Any, Any], dict[Any, Any]], opts: dict[str, int]
) -> None:
    run(*pair, "id INTEGER, code VARCHAR", ["id", "code"], bucketing="range", **opts)


def test_helper_types() -> None:
    assert isinstance(make_duck(), DbApiAdapter)
