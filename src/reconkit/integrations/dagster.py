"""Dagster asset-check helper.

Importing this module does not import Dagster; :func:`build_asset_check` does.

    from reconkit.integrations.dagster import build_asset_check

    loans_reconciled = build_asset_check(
        asset="warehouse_loans", config_path="reconkit.yml", check="loans"
    )
"""

from __future__ import annotations

from typing import Any

from reconkit.runner import CheckOutcome, run_config


def outcome_metadata(outcome: CheckOutcome) -> dict[str, Any]:
    """Flat, JSON-friendly metadata for an asset check result."""
    meta: dict[str, Any] = {
        "status": outcome.status,
        "summary": outcome.summary,
        "duration_seconds": round(outcome.duration_seconds, 3),
    }
    if outcome.diff is not None:
        meta.update(
            strategy=outcome.diff.strategy,
            missing_in_left=outcome.diff.missing_in_left,
            missing_in_right=outcome.diff.missing_in_right,
            changed=outcome.diff.changed,
            left_rows=outcome.diff.left.row_count,
            right_rows=outcome.diff.right.row_count,
        )
    if outcome.contract is not None:
        meta.update(
            rules_passed=outcome.contract.count("pass"),
            rules_failed=outcome.contract.count("fail") + outcome.contract.count("error"),
        )
    if outcome.error:
        meta["error"] = outcome.error
    return {k: v for k, v in meta.items() if v is not None}


def build_asset_check(
    *,
    asset: Any,
    config_path: str,
    check: str,
    name: str | None = None,
    blocking: bool = False,
    audit: bool = True,
) -> Any:
    """Return a Dagster asset check that runs one reconkit check or contract.

    The check passes only when the reconkit check passes. A check that cannot run
    (connection error) is reported as failed with the error in its metadata.
    """
    import dagster

    @dagster.asset_check(asset=asset, name=name or f"reconkit_{check}", blocking=blocking)  # type: ignore[untyped-decorator]
    def _reconkit_check() -> Any:
        outcome = run_config(config_path, [check], audit=audit).outcomes[0]
        return dagster.AssetCheckResult(
            passed=outcome.status == "pass", metadata=outcome_metadata(outcome)
        )

    return _reconkit_check
