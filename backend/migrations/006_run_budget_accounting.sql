ALTER TABLE runs
    ADD COLUMN elapsed_used_ms bigint NOT NULL DEFAULT 0 CHECK (elapsed_used_ms >= 0),
    ADD COLUMN elapsed_active_since timestamptz,
    ADD COLUMN budget_decision_id uuid;

CREATE TABLE run_budget_extensions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    idempotency_key varchar(200) NOT NULL CHECK (length(idempotency_key) > 0),
    caller_identity uuid NOT NULL,
    expected_revision integer NOT NULL CHECK (expected_revision >= 1),
    payload_hash char(64) NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    token_limit_before bigint NOT NULL CHECK (token_limit_before >= 0),
    token_limit_after bigint NOT NULL CHECK (token_limit_after >= 0),
    elapsed_limit_before_ms bigint NOT NULL CHECK (elapsed_limit_before_ms >= 0),
    elapsed_limit_after_ms bigint NOT NULL CHECK (elapsed_limit_after_ms >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, idempotency_key),
    CHECK (token_limit_after >= token_limit_before),
    CHECK (elapsed_limit_after_ms >= elapsed_limit_before_ms),
    CHECK (token_limit_after > token_limit_before
        OR elapsed_limit_after_ms > elapsed_limit_before_ms)
);

CREATE TRIGGER run_budget_extensions_immutable
    BEFORE UPDATE OR DELETE ON run_budget_extensions
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
