"""reconkit: cross-database table reconciliation and data contract checks."""

from reconkit.diff.engine import TableSpec, diff_tables
from reconkit.diff.model import ColumnDiff, DiffOptions, DiffResult, RowDiff, Tolerance

__version__ = "0.2.0"

__all__ = [
    "ColumnDiff",
    "DiffOptions",
    "DiffResult",
    "RowDiff",
    "TableSpec",
    "Tolerance",
    "__version__",
    "diff_tables",
]
