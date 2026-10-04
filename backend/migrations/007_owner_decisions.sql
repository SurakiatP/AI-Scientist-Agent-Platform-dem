-- Durable owner decisions: decision_id is bound to its run (and operation) when the
-- decision.required event is issued; resolution and idempotency receipt commit together.
CREATE TABLE owner_decisions (
    decision_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    operation_id varchar(200),
    FOREIGN KEY (run_id, operation_id) REFERENCES operations(run_id, operation_id),
    reason varchar(32) NOT NULL CHECK (reason IN ('unknown_outcome', 'budget_exhausted')),
    state varchar(16) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'resolved')),
    issued_at timestamptz NOT NULL DEFAULT now(),
    resolution jsonb,
    idempotency_key varchar(200) CHECK (idempotency_key IS NULL OR length(idempotency_key) > 0),
    payload_hash char(64) CHECK (payload_hash IS NULL OR payload_hash ~ '^[0-9a-f]{64}$'),
    resolved_at timestamptz,
    CHECK ((reason = 'unknown_outcome') = (operation_id IS NOT NULL)),
    CHECK (state = 'pending' OR resolved_at IS NOT NULL)
);
CREATE UNIQUE INDEX owner_decisions_one_pending_unknown
    ON owner_decisions (run_id, operation_id) WHERE state = 'pending' AND reason = 'unknown_outcome';
CREATE UNIQUE INDEX owner_decisions_idempotency
    ON owner_decisions (run_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TRIGGER owner_decisions_resolved_are_immutable BEFORE UPDATE OR DELETE ON owner_decisions
    FOR EACH ROW WHEN (OLD.state = 'resolved') EXECUTE FUNCTION reject_immutable_record_change();
