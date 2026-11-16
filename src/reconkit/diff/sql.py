"""SQL for one side of a diff. Everything engine-specific is delegated to the dialect."""

from __future__ import annotations

from collections.abc import Sequence

from reconkit.adapters.base import Dialect
from reconkit.diff.plan import HashPlan, Plan, RangePlan
from reconkit.types import Canon, ColumnInfo, TableRef

#: Sampling works on key-hash bits above those used for bucketing where possible.
_SAMPLE_SHIFT = 1 << 20
_SAMPLE_BASE = 10_000


class SideSql:
    def __init__(
        self,
        dialect: Dialect,
        table: TableRef,
        keys: Sequence[tuple[ColumnInfo, Canon]],
        values: Sequence[tuple[ColumnInfo, Canon]],
        *,
        where: str | None = None,
        naive_timezone: str | None = None,
        sample: float | None = None,
    ) -> None:
        self.d = dialect
        self.table = dialect.table(table)
        self.keys = list(keys)
        self.values = list(values)
        self.where = where
        self.naive_timezone = naive_timezone
        self.sample_cut = None if sample is None else max(1, round(sample * _SAMPLE_BASE))

    # -- expressions --------------------------------------------------------------------

    def norm(self, col: ColumnInfo, canon: Canon) -> str:
        return self.d.normalize(self.d.quote(col.name), col.type, canon, self.naive_timezone)

    def enc(self, col: ColumnInfo, canon: Canon) -> str:
        """Never-NULL encoding: 'N' for NULL, 'V' + canonical text otherwise."""
        return f"COALESCE('V' || {self.norm(col, canon)}, 'N')"

    def _hash_of(self, cols: Sequence[tuple[ColumnInfo, Canon]]) -> str:
        parts: list[str] = []
        for i, (col, canon) in enumerate(cols):
            if i:
                parts.append("'|'")
            parts.append(self.enc(col, canon))
        return self.d.md5_int(self.d.concat(parts))

    @property
    def key_hash(self) -> str:
        return self._hash_of(self.keys)

    @property
    def row_hash(self) -> str:
        return self._hash_of([*self.keys, *self.values])

    @property
    def range_key(self) -> str:
        return self.d.quote(self.keys[0][0].name)

    def _sample_pred(self, key_hash: str) -> str | None:
        if self.sample_cut is None:
            return None
        shifted = self.d.int_div(key_hash, str(_SAMPLE_SHIFT))
        return f"{self.d.mod(shifted, str(_SAMPLE_BASE))} < {self.sample_cut}"

    def _where(self, *extra: str | None) -> str:
        preds = [f"({self.where})"] if self.where else []
        preds += [p for p in extra if p]
        return f" WHERE {' AND '.join(preds)}" if preds else ""

    def _select_list(self) -> str:
        cols = [*self.keys, *self.values]
        return ", ".join(f"{self.norm(c, canon)} AS c{i}" for i, (c, canon) in enumerate(cols))

    def _range_pred(self, ranges: Sequence[tuple[int, int]]) -> str:
        k = self.range_key
        return "(" + " OR ".join(f"({k} >= {a} AND {k} < {b})" for a, b in ranges) + ")"

    # -- queries ------------------------------------------------------------------------

    def minmax_sql(self) -> str:
        """Bounds of the first key column, the number of NULL keys and the row count."""
        k = self.range_key
        nulls = " + ".join(
            f"SUM(CASE WHEN {self.d.quote(c.name)} IS NULL THEN 1 ELSE 0 END)" for c, _ in self.keys
        )
        return f"SELECT MIN({k}), MAX({k}), {nulls}, COUNT(*) FROM {self.table}{self._where()}"

    def count_sql(self) -> str:
        sample = self._sample_pred(self.key_hash)
        return f"SELECT COUNT(*) FROM {self.table}{self._where(sample)}"

    def duplicate_keys_sql(self) -> str:
        keys = ", ".join(self.d.quote(c.name) for c, _ in self.keys)
        return (
            f"SELECT COUNT(*) FROM (SELECT {keys} FROM {self.table}{self._where()} "
            f"GROUP BY {keys} HAVING COUNT(*) > 1) d"
        )

    def checksum_sql(self, plan: Plan, level: int, parents: Sequence[int] | None) -> str:
        """One row per non-empty bucket at ``level``: (bucket, row count, sum of row hashes).

        ``parents`` restricts the scan to the given buckets of ``level - 1``.
        """
        d = self.d
        agg = f"COUNT(*) AS n, {d.sum_hash('h')} AS s"
        if isinstance(plan, RangePlan):
            k = self.range_key
            if parents is None:
                scope = f"({k} >= {plan.lo} AND {k} <= {plan.hi})"
            else:
                scope = self._range_pred(plan.ranges(level - 1, parents))
            inner = (
                f"SELECT {d.to_bigint(k)} AS rk, {self.row_hash} AS h FROM {self.table}"
                f"{self._where(scope, self._sample_pred(self.key_hash))}"
            )
            bucket = d.int_div(f"rk - ({plan.lo})", str(plan.widths[level]))
            return f"SELECT {bucket} AS b, {agg} FROM ({inner}) t GROUP BY 1"
        inner = (
            f"SELECT {self.key_hash} AS kh, {self.row_hash} AS h FROM {self.table}{self._where()}"
        )
        preds = [self._sample_pred("kh")]
        if parents is not None:
            preds.append(self._hash_pred(plan, level - 1, parents))
        outer_where = " AND ".join(p for p in preds if p)
        bucket = d.mod("kh", str(plan.modulus(level)))
        return (
            f"SELECT {bucket} AS b, {agg} FROM ({inner}) t"
            f"{' WHERE ' + outer_where if outer_where else ''} GROUP BY 1"
        )

    def _hash_pred(self, plan: HashPlan, level: int, buckets: Sequence[int]) -> str:
        members = ", ".join(str(b) for b in sorted(buckets))
        return f"{self.d.mod('kh', str(plan.modulus(level)))} IN ({members})"

    def fetch_sql(self, plan: Plan, level: int, buckets: Sequence[int]) -> str:
        """Canonical rows (keys first) of the given buckets at ``level``."""
        if isinstance(plan, RangePlan):
            scope = self._range_pred(plan.ranges(level, buckets))
            sample = self._sample_pred(self.key_hash)
            return f"SELECT {self._select_list()} FROM {self.table}{self._where(scope, sample)}"
        n = len(self.keys) + len(self.values)
        inner = (
            f"SELECT {self._select_list()}, {self.key_hash} AS kh FROM {self.table}{self._where()}"
        )
        preds = [self._hash_pred(plan, level, buckets), self._sample_pred("kh")]
        outer_where = " AND ".join(p for p in preds if p)
        cols = ", ".join(f"c{i}" for i in range(n))
        return f"SELECT {cols} FROM ({inner}) t WHERE {outer_where}"

    def join_side_sql(self) -> str:
        """Derived table for the join strategy: canonical columns plus encoded join keys."""
        parts = [self._select_list()]
        parts += [f"{self.enc(c, canon)} AS j{i}" for i, (c, canon) in enumerate(self.keys)]
        sample = self._sample_pred(self.key_hash)
        return f"SELECT {', '.join(parts)} FROM {self.table}{self._where(sample)}"


def join_diff_sql(left: SideSql, right: SideSql) -> str:
    """Rows that are missing on one side or differ in at least one compared column.

    Returns left canonical columns, right canonical columns, then one presence flag per
    side. Encoded keys (never NULL) make NULL-safe equality possible with plain ``=``.
    """
    n_keys, n = len(left.keys), len(left.keys) + len(left.values)
    on = " AND ".join(f"l.j{i} = r.j{i}" for i in range(n_keys))
    differs = [
        f"COALESCE('V' || l.c{i}, 'N') <> COALESCE('V' || r.c{i}, 'N')" for i in range(n_keys, n)
    ]
    where = " OR ".join(["l.j0 IS NULL", "r.j0 IS NULL", *differs])
    cols = ", ".join([f"l.c{i}" for i in range(n)] + [f"r.c{i}" for i in range(n)])
    return (
        f"SELECT {cols}, l.j0, r.j0 FROM ({left.join_side_sql()}) l "
        f"FULL OUTER JOIN ({right.join_side_sql()}) r ON {on} WHERE {where}"
    )
