-- Existing scientific workspace objects are published only from verified receipts.
CREATE TABLE scientific_artifact_receipts (
    run_id uuid NOT NULL,
    tool_call_id varchar(128) NOT NULL,
    project_id uuid NOT NULL,
    checkpoint_id uuid NOT NULL REFERENCES checkpoints(id),
    artifact_id uuid NOT NULL,
    receipt_sha256 char(64) NOT NULL,
    PRIMARY KEY (run_id, tool_call_id),
    FOREIGN KEY (run_id, project_id) REFERENCES runs(id, project_id),
    FOREIGN KEY (artifact_id, project_id) REFERENCES artifacts(id, project_id)
);
CREATE TRIGGER scientific_receipts_are_immutable BEFORE UPDATE OR DELETE ON scientific_artifact_receipts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();

-- A single owner/profile job can serve multiple project requests without building twice.
CREATE TABLE profile_preparations (
    id uuid PRIMARY KEY,
    owner_identity uuid NOT NULL,
    profile_id varchar(200) NOT NULL,
    version varchar(200) NOT NULL,
    manifest_sha256 char(64) NOT NULL,
    state varchar(24) NOT NULL CHECK (state IN ('queued','building','checking','ready','blocked','failed','unknown')),
    stage varchar(24) NOT NULL CHECK (stage IN ('queued','context','image','compatibility','security','license','isolation','complete','owner_decision')),
    error_code varchar(200),
    evidence jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (state <> 'ready' OR (stage = 'complete' AND evidence IS NOT NULL))
);
CREATE UNIQUE INDEX one_active_profile_preparation ON profile_preparations(owner_identity, profile_id, version)
    WHERE state IN ('queued','building','checking','unknown');
CREATE TABLE preparation_requests (
    owner_identity uuid NOT NULL,
    request_id uuid NOT NULL,
    project_id uuid NOT NULL REFERENCES projects(id),
    job_id uuid NOT NULL REFERENCES profile_preparations(id),
    payload_sha256 char(64) NOT NULL,
    PRIMARY KEY (owner_identity, request_id)
);
CREATE TRIGGER preparation_requests_are_immutable BEFORE UPDATE OR DELETE ON preparation_requests
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
