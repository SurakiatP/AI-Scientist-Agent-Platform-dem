-- One-shot peer status reads reuse operation-scoped physical executor identities.
ALTER TABLE runtime_executors
    ADD COLUMN peer_reconciliation_attempt smallint
        CHECK (peer_reconciliation_attempt BETWEEN 1 AND 10),
    ADD COLUMN peer_reconciliation_started boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT runtime_peer_reconciliation_shape CHECK (
        (peer_reconciliation_attempt IS NULL AND NOT peer_reconciliation_started)
        OR (peer_reconciliation_attempt IS NOT NULL AND kind = 'dispatch' AND operation_id IS NOT NULL)
    );
CREATE UNIQUE INDEX runtime_peer_reconciliation_attempt_unique
    ON runtime_executors (run_id, operation_id, peer_reconciliation_attempt)
    WHERE peer_reconciliation_attempt IS NOT NULL;

CREATE FUNCTION guard_peer_reconciliation_executor() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.peer_reconciliation_attempt IS DISTINCT FROM OLD.peer_reconciliation_attempt
       OR (OLD.peer_reconciliation_started AND NOT NEW.peer_reconciliation_started)
       OR (NOT OLD.peer_reconciliation_started AND NEW.peer_reconciliation_started
           AND (OLD.state <> 'active' OR NEW.state <> 'active')) THEN
        RAISE EXCEPTION 'immutable_peer_reconciliation_executor';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER runtime_peer_reconciliation_executor_guard
    BEFORE UPDATE ON runtime_executors
    FOR EACH ROW EXECUTE FUNCTION guard_peer_reconciliation_executor();
