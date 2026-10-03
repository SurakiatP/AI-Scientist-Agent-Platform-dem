CREATE TABLE projects (
    id uuid PRIMARY KEY,
    name varchar(200) NOT NULL CHECK (length(name) > 0),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    instructions text NOT NULL DEFAULT ''
);
CREATE TABLE sessions (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    title varchar(200) NOT NULL,
    parent_session_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, project_id),
    FOREIGN KEY (parent_session_id, project_id) REFERENCES sessions(id, project_id)
);
CREATE TABLE messages (
    id uuid PRIMARY KEY, project_id uuid NOT NULL, session_id uuid NOT NULL,
    role varchar(24) NOT NULL, content text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (session_id, project_id) REFERENCES sessions(id, project_id)
);
CREATE TABLE artifacts (
    id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), run_id uuid,
    title varchar(300) NOT NULL, kind varchar(16) NOT NULL CHECK (kind IN ('report','table','plot','file')),
    object_key text NOT NULL, sha256 char(64) NOT NULL, size bigint NOT NULL CHECK (size >= 0),
    content_type varchar(255) NOT NULL, partial boolean NOT NULL DEFAULT false,
    UNIQUE (id, project_id)
);
CREATE TABLE findings (
    id uuid PRIMARY KEY, project_id uuid NOT NULL, session_id uuid NOT NULL,
    artifact_id uuid, text text NOT NULL, citation_ids uuid[] NOT NULL DEFAULT '{}',
    UNIQUE (id, project_id),
    FOREIGN KEY (session_id, project_id) REFERENCES sessions(id, project_id),
    FOREIGN KEY (artifact_id, project_id) REFERENCES artifacts(id, project_id)
);
CREATE TABLE sources (
    id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), metadata jsonb NOT NULL,
    UNIQUE (id, project_id)
);
CREATE TABLE citations (
    id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), source_id uuid,
    title varchar(1000) NOT NULL, authors jsonb NOT NULL DEFAULT '[]', year integer,
    identifier varchar(500), original_url text, access varchar(32), verification varchar(32),
    UNIQUE (id, project_id), FOREIGN KEY (source_id, project_id) REFERENCES sources(id, project_id)
);
CREATE TABLE finding_citations (
    finding_id uuid NOT NULL, citation_id uuid NOT NULL, project_id uuid NOT NULL,
    PRIMARY KEY (finding_id, citation_id),
    FOREIGN KEY (finding_id, project_id) REFERENCES findings(id, project_id),
    FOREIGN KEY (citation_id, project_id) REFERENCES citations(id, project_id)
);
CREATE TABLE file_versions (
    id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), filename varchar(255) NOT NULL,
    object_key text, size bigint NOT NULL CHECK (size >= 0), content_type varchar(255) NOT NULL,
    state varchar(16) NOT NULL CHECK (state IN ('uploading','preparing','ready','failed')),
    error_code varchar(100), sha256 char(64), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE runs (
    id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), session_id uuid NOT NULL,
    caller_identity uuid NOT NULL, submission_key varchar(200) NOT NULL, submission_hash char(64) NOT NULL,
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    state varchar(32) NOT NULL DEFAULT 'planning', stage varchar(200), waiting_reason varchar(100),
    error_code varchar(100), plan_digest char(64), latest_cursor bigint NOT NULL DEFAULT 0 CHECK (latest_cursor >= 0),
    usage_tokens bigint NOT NULL DEFAULT 0 CHECK (usage_tokens >= 0),
    reserved_tokens bigint NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0),
    planning_tokens bigint NOT NULL DEFAULT 0 CHECK (planning_tokens >= 0),
    token_limit bigint NOT NULL DEFAULT 0 CHECK (token_limit >= 0),
    elapsed_limit_ms bigint NOT NULL DEFAULT 0 CHECK (elapsed_limit_ms >= 0),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0), lease_expires_at timestamptz,
    cancel_requested boolean NOT NULL DEFAULT false,
    UNIQUE (id, project_id), UNIQUE (caller_identity, submission_key),
    FOREIGN KEY (session_id, project_id) REFERENCES sessions(id, project_id)
);
ALTER TABLE artifacts ADD CONSTRAINT artifacts_run_project_fk
    FOREIGN KEY (run_id, project_id) REFERENCES runs(id, project_id);
CREATE TABLE publication_requests (
    id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), publication_key varchar(200) NOT NULL,
    payload_hash char(64) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, publication_key)
);
CREATE TABLE input_snapshots (
    id uuid PRIMARY KEY, project_id uuid NOT NULL, run_id uuid NOT NULL, digest char(64) NOT NULL,
    manifest jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id, project_id),
    FOREIGN KEY (run_id, project_id) REFERENCES runs(id, project_id)
);
CREATE TABLE plan_revisions (
    run_id uuid NOT NULL, project_id uuid NOT NULL, revision integer NOT NULL CHECK (revision > 0),
    digest char(64) NOT NULL, plan jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, revision), UNIQUE (run_id, revision, project_id),
    FOREIGN KEY (run_id, project_id) REFERENCES runs(id, project_id)
);
CREATE TABLE approvals (
    id uuid PRIMARY KEY, run_id uuid NOT NULL, revision integer NOT NULL, project_id uuid NOT NULL,
    owner_identity uuid NOT NULL, plan_digest char(64) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, revision, project_id) REFERENCES plan_revisions(run_id, revision, project_id)
);
CREATE TABLE operations (
    id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs(id), operation_id varchar(200) NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 0), kind varchar(16) NOT NULL,
    payload_hash char(64) NOT NULL, state varchar(16) NOT NULL,
    reserve_tokens bigint NOT NULL CHECK (reserve_tokens >= 0), usage_tokens bigint NOT NULL DEFAULT 0 CHECK (usage_tokens >= 0),
    result jsonb, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (run_id, operation_id)
);
CREATE TABLE checkpoints (
    id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES runs(id), revision integer NOT NULL,
    manifest jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE events (
    run_id uuid NOT NULL REFERENCES runs(id), sequence bigint NOT NULL CHECK (sequence > 0),
    revision integer NOT NULL, occurred_at timestamptz NOT NULL DEFAULT now(), kind varchar(64) NOT NULL,
    payload jsonb NOT NULL, PRIMARY KEY (run_id, sequence)
);
CREATE TABLE credentials (
    id uuid PRIMARY KEY, project_id uuid REFERENCES projects(id), label varchar(200) NOT NULL,
    provider varchar(100) NOT NULL, encrypted_value bytea NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE owner_sessions (
    id uuid PRIMARY KEY, token_hash char(64) NOT NULL UNIQUE, csrf_hash char(64) NOT NULL,
    expires_at timestamptz NOT NULL, revoked_at timestamptz
);
CREATE TABLE access_tokens (
    id uuid PRIMARY KEY, token_hash char(64) NOT NULL UNIQUE, owner_identity uuid NOT NULL,
    expires_at timestamptz, revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE access_grants (
    token_id uuid NOT NULL REFERENCES access_tokens(id), project_id uuid NOT NULL REFERENCES projects(id),
    actions text[] NOT NULL, PRIMARY KEY (token_id, project_id)
);
CREATE TABLE delegations (
    id uuid PRIMARY KEY, project_id uuid NOT NULL REFERENCES projects(id), owner_identity uuid NOT NULL,
    peer_id uuid NOT NULL, actions text[] NOT NULL, revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (project_id, owner_identity, peer_id)
);
CREATE FUNCTION reject_immutable_record_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable record cannot be changed';
END;
$$;
CREATE TRIGGER input_snapshots_are_immutable BEFORE UPDATE OR DELETE ON input_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
CREATE TRIGGER plan_revisions_are_immutable BEFORE UPDATE OR DELETE ON plan_revisions
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
CREATE TRIGGER approvals_are_immutable BEFORE UPDATE OR DELETE ON approvals
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
CREATE TRIGGER checkpoints_are_immutable BEFORE UPDATE OR DELETE ON checkpoints
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
CREATE TRIGGER events_are_immutable BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
