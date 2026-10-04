"""Assertions shared by ignored native B5 fault-acceptance harnesses."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text


def provider_attempts(db: Any, run_id: UUID, operation_id: str) -> int:
    """Count fixture-side durable attempts, independent of application ledger."""
    return int(
        db.execute(
            text(
                """
                SELECT count(*)
                FROM b5_fixture_provider_attempts
                WHERE run_id = :run AND operation_id = :operation
                """
            ),
            {"run": run_id, "operation": operation_id},
        ).scalar_one()
    )


def assert_unknown_waiting(db: Any, run_id: UUID) -> dict[str, Any]:
    """Require one unknown LLM operation with held budget and one remote attempt."""
    run = db.execute(
        text(
            """
            SELECT state, waiting_reason, usage_tokens, reserved_tokens, generation
            FROM runs WHERE id = :run
            """
        ),
        {"run": run_id},
    ).mappings().one()
    if (
        run["state"] != "waiting_input"
        or run["waiting_reason"] != "unknown_outcome"
        or run["usage_tokens"] != 0
        or run["reserved_tokens"] <= 0
    ):
        raise AssertionError("unknown outcome did not retain owner wait, zero usage and reservation")

    operations = db.execute(
        text(
            """
            SELECT operation_id, kind, generation, state, reserve_tokens, usage_tokens, result
            FROM operations WHERE run_id = :run
            ORDER BY created_at, operation_id
            """
        ),
        {"run": run_id},
    ).mappings().all()
    if len(operations) != 1:
        raise AssertionError(f"expected one durable LLM operation, got {len(operations)}")
    operation = operations[0]
    result = operation["result"] or {}
    if (
        operation["kind"] != "llm"
        or operation["state"] != "unknown"
        or operation["generation"] != run["generation"]
        or operation["reserve_tokens"] <= 0
        or operation["usage_tokens"] != 0
        or result.get("usage_known") is not False
        or result.get("ref") is not None
        or run["reserved_tokens"] != operation["reserve_tokens"]
    ):
        raise AssertionError("unknown-operation ledger has usage, result reference or reservation mismatch")

    attempts = provider_attempts(db, run_id, operation["operation_id"])
    if attempts != 1:
        raise AssertionError(f"fixture durable provider-attempt count is {attempts}, expected 1")
    total_attempts = int(
        db.execute(
            text("SELECT count(*) FROM b5_fixture_provider_attempts WHERE run_id = :run"),
            {"run": run_id},
        ).scalar_one()
    )
    if total_attempts != 1:
        raise AssertionError(f"fixture total provider-attempt count is {total_attempts}, expected 1")

    return {
        "run": {
            key: run[key]
            for key in ("state", "waiting_reason", "usage_tokens", "reserved_tokens", "generation")
        },
        "operation_id": operation["operation_id"],
        "operation": {
            key: operation[key]
            for key in ("kind", "generation", "state", "reserve_tokens", "usage_tokens")
        },
        "result_ref_present": (operation["result"] or {}).get("ref") is not None,
        "provider_attempts": attempts,
        "total_provider_attempts": total_attempts,
    }


def assert_no_resend(
    db: Any,
    run_id: UUID,
    baseline: dict[str, Any],
    expected_attempts: int = 1,
) -> None:
    """Fail if a recovery/restart path sends the same operation again."""
    current = assert_unknown_waiting(db, run_id)
    if current["operation_id"] != baseline["operation_id"]:
        raise AssertionError("unknown operation identity changed across recovery/restart")
    if current["provider_attempts"] != expected_attempts:
        raise AssertionError(
            f"provider attempt count changed across recovery/restart: "
            f"{current['provider_attempts']} != {expected_attempts}"
        )
    for section, fields in {
        "run": ("usage_tokens", "reserved_tokens", "generation", "state", "waiting_reason"),
        "operation": ("state", "reserve_tokens", "usage_tokens"),
    }.items():
        if any(current[section][field] != baseline[section][field] for field in fields):
            raise AssertionError(f"{section} accounting/state changed across recovery/restart")
