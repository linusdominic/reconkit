"""Bucket plans: how the key space is cut into buckets at each bisection level.

Both plans expose the same shape. Level 0 covers the whole table; every bucket at level
``d + 1`` lies inside exactly one bucket at level ``d`` (its parent).

* :class:`RangePlan` cuts an integer key column into equal-width ranges. Predicates are
  plain range filters on the raw column, so the database can use the primary-key index
  to read only mismatching ranges.
* :class:`HashPlan` buckets rows by ``hash(key) mod M`` with a modulus that grows by the
  bisection factor per level. It works for any key type (uuid, text, composite) but each
  level is a full scan, because no index is ordered by the hash.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: Largest modulus used for hash bucketing; hashes are 60 bits wide.
_MAX_MODULUS = 1 << 56


@dataclass(frozen=True)
class RangePlan:
    lo: int
    hi: int
    #: Bucket width per level; each width divides the previous one, the last is 1.
    widths: tuple[int, ...]

    @classmethod
    def _from_width(cls, lo: int, hi: int, factor: int, width: int, round_up: bool) -> RangePlan:
        """Plan whose level-0 width is ``width`` rounded to the form ``c * factor**m``."""
        power = 1
        while power * factor <= width:
            power *= factor
        c = -(-width // power) if round_up else width // power
        widths = []
        w = c * power
        while w > c:
            widths.append(w)
            w //= factor
        widths.append(c)
        if c > 1:
            widths.append(1)
        return cls(lo, hi, tuple(widths))

    @classmethod
    def build(cls, lo: int, hi: int, factor: int, initial_buckets: int) -> RangePlan:
        """Plan with at most ``initial_buckets`` buckets at level 0."""
        span = hi - lo + 1
        return cls._from_width(lo, hi, factor, -(-span // initial_buckets), round_up=True)

    @classmethod
    def for_rows(
        cls, lo: int, hi: int, factor: int, rows: int, threshold: int, max_buckets: int = 1 << 16
    ) -> RangePlan:
        """Plan sized from the row count.

        The first pass scans the whole table whatever the bucket count, so finer buckets
        are nearly free: one small result row each. Level-0 buckets are therefore made
        narrow enough to hold about ``threshold`` rows at the table's average key
        density, so a mismatching bucket can be downloaded straight away, unless that
        would need more than ``max_buckets`` buckets. Past that cap a mismatching bucket
        is split once more, reading only its own key range.
        """
        span = hi - lo + 1
        width = min(span, max(1, threshold * span // max(rows, 1)))
        plan = cls._from_width(lo, hi, factor, width, round_up=False)
        if -(-span // plan.widths[0]) > max_buckets:
            return cls.build(lo, hi, factor, max_buckets)
        return plan

    def is_last(self, level: int) -> bool:
        return level >= len(self.widths) - 1

    def parent(self, level: int, bucket: int) -> int:
        return bucket // (self.widths[level - 1] // self.widths[level])

    def bounds(self, level: int, bucket: int) -> tuple[int, int]:
        """Half-open key range ``[start, end)`` of a bucket."""
        width = self.widths[level]
        return self.lo + bucket * width, self.lo + (bucket + 1) * width

    def ranges(self, level: int, buckets: Sequence[int]) -> list[tuple[int, int]]:
        """Key ranges covering ``buckets``, with adjacent buckets merged."""
        merged: list[tuple[int, int]] = []
        for bucket in sorted(buckets):
            start, end = self.bounds(level, bucket)
            if merged and merged[-1][1] == start:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        return merged


@dataclass(frozen=True)
class HashPlan:
    factor: int
    initial_buckets: int

    def modulus(self, level: int) -> int:
        return int(self.initial_buckets * self.factor**level)

    def is_last(self, level: int) -> bool:
        return self.modulus(level + 1) > _MAX_MODULUS

    def parent(self, level: int, bucket: int) -> int:
        return bucket % self.modulus(level - 1)


def auto_hash_buckets(rows: int, threshold: int, max_buckets: int = 1 << 18) -> int:
    """Level-0 modulus for hash bucketing, sized from the row count.

    Hash buckets vary in size around their mean, and a bucket over the threshold costs
    another full scan, so the mean is set to half the threshold. The cap is higher than
    for range bucketing for the same reason: a bigger checksum result is cheaper than an
    extra scan.
    """
    return max(1, min(-(-rows // max(1, threshold // 2)), max_buckets))


Plan = RangePlan | HashPlan
