"""PostgreSQL-clock accounting and durable research-budget decisions."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session


EventWriter = Callable[[Session, UUID, int, str, dict[str, Any]], None]
_TERMINAL = {"completed", "failed", "canceled", "rejected"}


def _field(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    try:
        return row[name]
    except (KeyError, TypeError):
        return getattr(row, name)


def effective_elapsed_ms(db: Session, run_id: UUID) -> int:
    """Return persisted active milliseconds plus the current PG-clock interval."""
    return int(db.execute(text("""
        SELECT elapsed_used_ms + CASE
            WHEN elapsed_active_since IS NULL THEN 0
            ELSE GREATEST(0, FLOOR(EXTRACT(EPOCH FROM
                (clock_timestamp() - elapsed_active_since)) * 1000)::bigint)
        END
        FROM runs WHERE id=:run
    """), {"run": run_id}).scalar_one())


def start_active_interval(db: Session, run_id: UUID) -> None:
    """Start or retain the active interval using PostgreSQL's wall clock."""
    changed = db.execute(text("""
        UPDATE runs
        SET elapsed_active_since=COALESCE(elapsed_active_since, clock_timestamp())
        WHERE id=:run AND state='running'
    """), {"run": run_id}).rowcount
    if changed != 1:
        raise ValueError("cannot start elapsed accounting for a non-running run")


def settle_active_interval(db: Session, run_id: UUID) -> int:
    """Freeze elapsed time after the caller proves every executor inactive."""
    return int(db.execute(text("""
        WITH sample AS MATERIALIZED (SELECT clock_timestamp() AS at)
        UPDATE runs AS r
        SET elapsed_used_ms = r.elapsed_used_ms + CASE
                WHEN r.elapsed_active_since IS NULL THEN 0
                ELSE GREATEST(0, FLOOR(EXTRACT(EPOCH FROM
                    (sample.at - r.elapsed_active_since)) * 1000)::bigint)
            END,
            elapsed_active_since = NULL
        FROM sample
        WHERE r.id=:run
        RETURNING r.elapsed_used_ms
    """), {"run": run_id}).scalar_one())


def budget_exhausted(db: Session, run: Any, *, reserve_tokens: int = 0) -> bool:
    """Check elapsed budget and whether usage leaves room for another attempt."""
    token_limit = int(_field(run, "token_limit"))
    token_total = (
        int(_field(run, "usage_tokens"))
        + int(_field(run, "reserved_tokens"))
        + reserve_tokens
    )
    token_exhausted = token_total > token_limit or (
        reserve_tokens == 0 and token_total >= token_limit
    )
    return token_exhausted or effective_elapsed_ms(db, _field(run, "id")) >= int(
        _field(run, "elapsed_limit_ms")
    )

def mark_budget_wait(
    db: Session,
    run_id: UUID,
    revision: int,
    emit_event: EventWriter,
) -> UUID | None:
    """Persist one owner decision request unless a stronger state already won."""
    row = db.execute(text("""
        SELECT state, waiting_reason, cancel_requested, budget_decision_id
        FROM runs WHERE id=:run FOR UPDATE
    """), {"run": run_id}).mappings().one_or_none()
    if row is None or row["state"] in _TERMINAL or row["cancel_requested"]:
        return None
    if row["state"] == "waiting_input" and row["waiting_reason"] != "budget_exhausted":
        return None
    if row["state"] not in {"running", "queued", "recovering", "waiting_input"}:
        return None

    decision_id = row["budget_decision_id"] or uuid4()
    already_waiting = row["state"] == "waiting_input" and row["waiting_reason"] == "budget_exhausted"
    db.execute(text("""
        UPDATE runs SET state='waiting_input', waiting_reason='budget_exhausted',
            budget_decision_id=:decision, lease_expires_at=NULL
        WHERE id=:run
    """), {"run": run_id, "decision": decision_id})
    if not already_waiting:
        emit_event(db, run_id, revision, "run.state", {"state": "waiting_input"})
    if row["budget_decision_id"] is None:
        emit_event(db, run_id, revision, "decision.required", {
            "decision_id": decision_id, "reason": "budget_exhausted",
        })
    return decision_id
