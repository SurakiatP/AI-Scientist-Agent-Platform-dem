ALTER TABLE peer_outbound_receipts
    ADD COLUMN reconciliation_attempts integer NOT NULL DEFAULT 0
        CHECK (reconciliation_attempts BETWEEN 0 AND 10);

CREATE OR REPLACE FUNCTION guard_peer_outbound_receipt_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (NEW.run_id, NEW.project_id, NEW.operation_id, NEW.release_id,
        NEW.peer_id, NEW.message_id, NEW.endpoint_fingerprint,
        NEW.parameters_sha256, NEW.created_at)
       IS DISTINCT FROM
       (OLD.run_id, OLD.project_id, OLD.operation_id, OLD.release_id,
        OLD.peer_id, OLD.message_id, OLD.endpoint_fingerprint,
        OLD.parameters_sha256, OLD.created_at) THEN
        RAISE EXCEPTION 'peer outbound receipt binding immutable';
    END IF;

    IF OLD.remote_task_id IS NOT NULL
       AND NEW.remote_task_id IS DISTINCT FROM OLD.remote_task_id THEN
        RAISE EXCEPTION 'peer remote task identity cannot be replaced';
    END IF;
    IF OLD.remote_context_id IS NOT NULL
       AND NEW.remote_context_id IS DISTINCT FROM OLD.remote_context_id THEN
        RAISE EXCEPTION 'peer remote context identity cannot be replaced';
    END IF;
    IF OLD.state IS DISTINCT FROM NEW.state
       AND NOT (
           (OLD.state = 'prepared' AND NEW.state IN ('unknown', 'accepted'))
           OR (OLD.state = 'unknown' AND NEW.state = 'accepted')
       ) THEN
        RAISE EXCEPTION 'peer outbound receipt state transition invalid';
    END IF;
    IF NEW.reconciliation_attempts NOT IN
       (OLD.reconciliation_attempts, OLD.reconciliation_attempts + 1) THEN
        RAISE EXCEPTION 'peer reconciliation attempts must increase by one';
    END IF;

    NEW.updated_at = now();
    RETURN NEW;
END;
$$;
