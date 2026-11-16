from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from reconkit.diff.plan import HashPlan, RangePlan, auto_hash_buckets
from reconkit.errors import ConfigError
from reconkit.types import TableRef, unquote


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("loans", ("loans",)),
        ("public.loans", ("public", "loans")),
        ('db.public."Mixed Case"', ("db", "public", '"Mixed Case"')),
        ('"a.b".t', ('"a.b"', "t")),
        ("`proj-1.ds`.t", ("`proj-1.ds`", "t")),
    ],
)
def test_table_ref_parse(text: str, parts: tuple[str, ...]) -> None:
    ref = TableRef.parse(text)
    assert ref.parts == parts
    assert str(ref) == text


@pytest.mark.parametrize(
    "text", ["", "a b", "t; DROP TABLE x", "a.b.c.d", "t--", "a..b", "x'y", "(select 1)"]
)
def test_table_ref_rejects_anything_but_identifiers(text: str) -> None:
    with pytest.raises(ConfigError):
        TableRef.parse(text)


def test_unquote() -> None:
    assert unquote('"a""b"') == 'a"b'
    assert unquote("`x`") == "x"
    assert unquote("plain") == "plain"


@given(
    lo=st.integers(-(10**12), 10**12),
    span=st.integers(1, 10**13),
    factor=st.integers(2, 64),
    initial=st.integers(1, 5000),
)
def test_range_plan_invariants(lo: int, span: int, factor: int, initial: int) -> None:
    hi = lo + span - 1
    plan = RangePlan.build(lo, hi, factor, initial)
    widths = plan.widths
    # Level 0 never has more buckets than asked for, and covers the whole key range.
    assert -(-span // widths[0]) <= initial
    assert plan.bounds(0, 0)[0] == lo
    assert plan.bounds(0, (hi - lo) // widths[0])[1] > hi
    # Each level refines the previous one exactly, down to single keys.
    assert widths[-1] == 1
    for coarse, fine in itertools.pairwise(widths):
        assert coarse % fine == 0
        assert 1 < coarse // fine <= max(factor, initial * factor)
    assert plan.is_last(len(widths) - 1)
    assert not plan.is_last(len(widths) - 2) or len(widths) == 1


@given(
    span=st.integers(1, 10**6),
    factor=st.integers(2, 16),
    initial=st.integers(1, 64),
    data=st.data(),
)
def test_range_plan_parent_contains_child(
    span: int, factor: int, initial: int, data: st.DataObject
) -> None:
    plan = RangePlan.build(0, span - 1, factor, initial)
    key = data.draw(st.integers(0, span - 1))
    for level in range(1, len(plan.widths)):
        child = key // plan.widths[level]
        parent = plan.parent(level, child)
        assert parent == key // plan.widths[level - 1]
        c0, c1 = plan.bounds(level, child)
        p0, p1 = plan.bounds(level - 1, parent)
        assert p0 <= c0 <= key < c1 <= p1


def test_range_plan_merges_adjacent_buckets() -> None:
    plan = RangePlan.build(0, 999, 10, 10)
    assert plan.widths == (100, 10, 1)
    assert plan.ranges(0, [3, 1, 2, 7]) == [(100, 400), (700, 800)]


def test_hash_plan() -> None:
    plan = HashPlan(factor=4, initial_buckets=8)
    assert [plan.modulus(i) for i in range(3)] == [8, 32, 128]
    # A level-1 bucket's parent is its residue under the level-0 modulus.
    assert plan.parent(1, 29) == 29 % 8
    assert not plan.is_last(0)
    assert plan.is_last(40)


def test_auto_hash_buckets() -> None:
    assert auto_hash_buckets(1_000_000, 64) == 31_250  # half the threshold per bucket
    assert auto_hash_buckets(0, 64) == 1
    assert auto_hash_buckets(100, 64) == 4
    assert auto_hash_buckets(100, 1) == 100
    assert auto_hash_buckets(10**9, 64) == 262_144  # capped


def test_range_plan_sized_from_rows() -> None:
    # Dense keys: level-0 buckets hold at most `threshold` keys, so no second level.
    dense = RangePlan.for_rows(1, 1_000_000, 32, rows=999_667, threshold=64)
    assert dense.widths == (64, 2, 1)
    # Sparse keys: wider buckets with the same expected row count.
    sparse = RangePlan.for_rows(1, 10_000_000, 32, rows=1_000_000, threshold=64)
    assert sparse.widths[0] == 640
    # Large tables hit the cap on level-0 buckets and get one more level instead.
    big = RangePlan.for_rows(1, 100_000_000, 32, rows=100_000_000, threshold=64)
    assert 32_768 < -(-100_000_000 // big.widths[0]) <= 65_536
    assert RangePlan.for_rows(5, 5, 32, rows=0, threshold=64).widths == (1,)


@given(
    span=st.integers(1, 10**12),
    rows=st.integers(0, 10**9),
    factor=st.integers(2, 64),
    threshold=st.integers(1, 5000),
)
def test_range_plan_for_rows_invariants(span: int, rows: int, factor: int, threshold: int) -> None:
    plan = RangePlan.for_rows(0, span - 1, factor, rows, threshold)
    assert -(-span // plan.widths[0]) <= 1 << 16
    assert plan.widths[-1] == 1
    for coarse, fine in itertools.pairwise(plan.widths):
        assert coarse % fine == 0 and coarse > fine
