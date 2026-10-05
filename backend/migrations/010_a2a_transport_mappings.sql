-- Transport identities supplement the domain run ledger; they never authorize work.
ALTER TABLE runs ADD CONSTRAINT runs_protocol_session_binding UNIQUE (id, project_id, session_id);

CREATE TABLE a2a_contexts (
    id uuid PRIMARY KEY,
    caller_id uuid NOT NULL REFERENCES access_tokens(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    session_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (session_id, project_id) REFERENCES sessions(id, project_id),
    UNIQUE (id, caller_id, project_id, session_id)
);

CREATE TABLE a2a_tasks (
    run_id uuid PRIMARY KEY,
    context_id uuid NOT NULL,
    caller_id uuid NOT NULL,
    project_id uuid NOT NULL,
    session_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (context_id, caller_id, project_id, session_id)
        REFERENCES a2a_contexts(id, caller_id, project_id, session_id),
    FOREIGN KEY (run_id, project_id, session_id) REFERENCES runs(id, project_id, session_id),
    UNIQUE (run_id, context_id, caller_id)
);

CREATE TABLE a2a_messages (
    caller_id uuid NOT NULL,
    message_id varchar(200) NOT NULL CHECK (length(message_id) > 0),
    payload_hash char(64) NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    run_id uuid NOT NULL,
    context_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (caller_id, message_id),
    FOREIGN KEY (run_id, context_id, caller_id) REFERENCES a2a_tasks(run_id, context_id, caller_id)
);

CREATE TRIGGER a2a_contexts_are_immutable BEFORE UPDATE OR DELETE ON a2a_contexts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
CREATE TRIGGER a2a_tasks_are_immutable BEFORE UPDATE OR DELETE ON a2a_tasks
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
CREATE TRIGGER a2a_messages_are_immutable BEFORE UPDATE OR DELETE ON a2a_messages
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
