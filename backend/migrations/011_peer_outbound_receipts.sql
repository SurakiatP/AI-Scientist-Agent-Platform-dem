CREATE TABLE peer_outbound_receipts (
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    operation_id varchar(200) NOT NULL CHECK (length(operation_id) > 0),
    release_id uuid NOT NULL,
    peer_id uuid NOT NULL,
    message_id varchar(200) NOT NULL CHECK (length(message_id) > 0),
    endpoint_fingerprint char(64) NOT NULL CHECK (endpoint_fingerprint ~ '^[0-9a-f]{64}$'),
    parameters_sha256 char(64) NOT NULL CHECK (parameters_sha256 ~ '^[0-9a-f]{64}$'),
    remote_task_id varchar(200),
    remote_context_id varchar(200),
    state varchar(16) NOT NULL DEFAULT 'prepared' CHECK (state IN ('prepared', 'unknown', 'accepted')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, operation_id),
    FOREIGN KEY (run_id, project_id) REFERENCES runs(id, project_id),
    FOREIGN KEY (run_id, operation_id) REFERENCES operations(run_id, operation_id),
    CHECK (remote_task_id IS NULL OR length(remote_task_id) > 0),
    CHECK (remote_context_id IS NULL OR length(remote_context_id) > 0),
    CHECK (state <> 'accepted' OR remote_task_id IS NOT NULL)
);

CREATE FUNCTION guard_peer_outbound_receipt_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.run_id, NEW.project_id, NEW.operation_id, NEW.release_id, NEW.peer_id,
        NEW.message_id, NEW.endpoint_fingerprint, NEW.parameters_sha256, NEW.created_at)
       IS DISTINCT FROM
       (OLD.run_id, OLD.project_id, OLD.operation_id, OLD.release_id, OLD.peer_id,
        OLD.message_id, OLD.endpoint_fingerprint, OLD.parameters_sha256, OLD.created_at) THEN
        RAISE EXCEPTION 'peer outbound receipt binding is immutable';
    END IF;
    IF OLD.remote_task_id IS NOT NULL AND NEW.remote_task_id IS DISTINCT FROM OLD.remote_task_id THEN
        RAISE EXCEPTION 'peer remote task identity cannot be replaced';
    END IF;
    IF OLD.remote_context_id IS NOT NULL AND NEW.remote_context_id IS DISTINCT FROM OLD.remote_context_id THEN
        RAISE EXCEPTION 'peer remote context identity cannot be replaced';
    END IF;
    IF OLD.state IS DISTINCT FROM NEW.state AND NOT (
        (OLD.state = 'prepared' AND NEW.state IN ('unknown', 'accepted')) OR
        (OLD.state = 'unknown' AND NEW.state = 'accepted')
    ) THEN
        RAISE EXCEPTION 'peer outbound receipt state cannot move backward';
    END IF;
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER peer_outbound_receipts_update_guard
    BEFORE UPDATE ON peer_outbound_receipts
    FOR EACH ROW EXECUTE FUNCTION guard_peer_outbound_receipt_update();

CREATE TRIGGER peer_outbound_receipts_are_not_deleted
    BEFORE DELETE ON peer_outbound_receipts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
