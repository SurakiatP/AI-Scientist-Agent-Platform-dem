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

def remaining_tokens(run: Any) -> int:
    """ADR-012 snapshot: effective token limit minus usage minus held reservations."""
    return max(0, int(_field(run, "token_limit")) - int(_field(run, "usage_tokens"))
               - int(_field(run, "reserved_tokens")))


def mark_budget_wait(
    db: Session,
    run_id: UUID,
    revision: int,
    emit_event: EventWriter,
    *,
    reserve_tokens: int = 0,
) -> UUID | None:
    """Persist one owner decision request unless a stronger state already won."""
    row = db.execute(text("""
        SELECT state, waiting_reason, cancel_requested, budget_decision_id,
               usage_tokens, reserved_tokens, token_limit, elapsed_limit_ms
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
        # Amounts mirror budget_exhausted(). Known limits: a reserve-0 wait (claim, recover, finalize,
        # post-extension re-check) cannot know the next request's reserve, so its amount restores only
        # 1 token and the next refusal raises a fresh decision. required_elapsed_ms is sampled while the
        # active interval may still be open (settled only after fencing), so it is a lower bound; an
        # extension that falls short re-raises a fresh decision.
        needed = reserve_tokens if reserve_tokens > 0 else 1
        emit_event(db, run_id, revision, "decision.required", {
            "decision_id": decision_id, "reason": "budget_exhausted",
            "required_tokens": max(0, row["usage_tokens"] + row["reserved_tokens"] + needed - row["token_limit"]),
            "required_elapsed_ms": max(0, effective_elapsed_ms(db, run_id) - row["elapsed_limit_ms"] + 1),
        })
    return decision_id


def confirm_unknown_usage(
    db: Session, run_id: UUID, operation: Any, usage_tokens: int, emit_event: EventWriter
) -> None:
    """Owner-confirmed settlement of one unknown operation: release exactly its reservation, add confirmed usage.

    Caller holds the run and operation row locks and has proven quiescence. Usage may not exceed the
    operation's reserve (hard cap), so the run never overshoots its limit by confirmation.
    """
    reserve = int(operation.reserve_tokens)
    if not 0 <= usage_tokens <= reserve:
        raise ValueError("confirmed usage exceeds the operation reserve")
    done = db.execute(text("""
        UPDATE operations SET state='committed', usage_tokens=:usage,
            result = result || jsonb_build_object('owner_confirmed_usage', true, 'usage_known', true, 'usage_tokens', CAST(:usage AS integer))
        WHERE id=:id AND state='unknown'
    """), {"usage": usage_tokens, "id": operation.id}).rowcount
    if done != 1:
        raise ValueError("operation is no longer unknown")
    held = db.execute(text("""
        UPDATE runs SET reserved_tokens = reserved_tokens - :reserve, usage_tokens = usage_tokens + :usage
        WHERE id=:run AND reserved_tokens >= :reserve
    """), {"reserve": reserve, "usage": usage_tokens, "run": run_id}).rowcount
    if held != 1:
        raise ValueError("run does not hold the operation reservation")
    row = db.execute(text("SELECT revision, usage_tokens, reserved_tokens, token_limit FROM runs WHERE id=:run"),
                     {"run": run_id}).one()
    emit_event(db, run_id, row.revision, "usage.updated", {
        "usage_tokens": row.usage_tokens, "reserved_tokens": row.reserved_tokens, "token_limit": row.token_limit})
