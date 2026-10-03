-- Checkpoint sequence and durable acknowledgements share the locked run boundary.
ALTER TABLE checkpoints ADD CONSTRAINT checkpoints_run_revision_unique UNIQUE (run_id, revision);
ALTER TABLE checkpoints ADD CONSTRAINT checkpoints_identity_revision_unique UNIQUE (id, run_id, revision);

CREATE TABLE checkpoint_boundaries (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    boundary_id uuid NOT NULL,
    checkpoint_revision integer NOT NULL CHECK (checkpoint_revision > 0),
    generation bigint NOT NULL CHECK (generation > 0),
    expected_checkpoint_revision integer NOT NULL CHECK (expected_checkpoint_revision >= 0),
    payload_hash char(64) NOT NULL CHECK (payload_hash ~ '^[a-f0-9]{64}$'),
    checkpoint_id uuid NOT NULL,
    ack jsonb NOT NULL CHECK (jsonb_typeof(ack) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, boundary_id),
    UNIQUE (run_id, checkpoint_revision),
    CHECK (checkpoint_revision = expected_checkpoint_revision + 1),
    FOREIGN KEY (checkpoint_id, run_id, checkpoint_revision) REFERENCES checkpoints(id, run_id, revision)
);
CREATE TRIGGER checkpoint_boundary_is_immutable BEFORE UPDATE ON checkpoint_boundaries
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();

-- Only the trusted supervisor may populate these records; worker APIs never do.
-- A physical identity is bound once, so a replacement cannot inherit a death proof.
CREATE TABLE runtime_executors (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    generation bigint NOT NULL CHECK (generation > 0),
    kind varchar(16) NOT NULL CHECK (kind IN ('worker', 'dispatch')),
    operation_id varchar(200),
    process_incarnation uuid NOT NULL,
    container_id varchar(64),
    engine_id varchar(200),
    state varchar(16) NOT NULL CHECK (state IN ('starting', 'active', 'fencing', 'inactive', 'unknown')),
    proof jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(proof) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (run_id, generation, kind, operation_id),
    CHECK (kind = 'dispatch' OR operation_id IS NULL),
    CHECK (state <> 'active' OR (container_id IS NOT NULL AND engine_id IS NOT NULL)),
    CHECK (container_id IS NULL OR container_id ~ '^[a-f0-9]{64}$'),
    FOREIGN KEY (run_id, operation_id) REFERENCES operations(run_id, operation_id)
);

CREATE FUNCTION guard_executor_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.run_id, NEW.generation, NEW.kind, NEW.operation_id, NEW.process_incarnation, NEW.created_at)
        IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.generation, OLD.kind, OLD.operation_id, OLD.process_incarnation, OLD.created_at)
       OR (OLD.container_id IS NOT NULL AND NEW.container_id IS DISTINCT FROM OLD.container_id)
       OR (OLD.engine_id IS NOT NULL AND NEW.engine_id IS DISTINCT FROM OLD.engine_id)
       OR ((OLD.container_id IS NULL AND NEW.container_id IS NOT NULL
            OR OLD.engine_id IS NULL AND NEW.engine_id IS NOT NULL) AND OLD.state <> 'starting') THEN
        RAISE EXCEPTION 'runtime executor physical identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER runtime_executor_identity_is_immutable BEFORE UPDATE ON runtime_executors
    FOR EACH ROW EXECUTE FUNCTION guard_executor_identity();
