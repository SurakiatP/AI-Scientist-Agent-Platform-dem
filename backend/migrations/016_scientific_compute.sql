ALTER TABLE runtime_executors
    ADD COLUMN compute_operation_id varchar(200);

ALTER TABLE runtime_executors
    DROP CONSTRAINT runtime_executors_kind_check,
    DROP CONSTRAINT runtime_executors_check,
    DROP CONSTRAINT runtime_executors_run_id_generation_kind_operation_id_key;

ALTER TABLE runtime_executors
    ADD CONSTRAINT runtime_executors_kind_check
        CHECK (kind IN ('worker', 'dispatch', 'compute')),
    ADD CONSTRAINT runtime_executors_operation_identity_check
        CHECK (
            (kind = 'worker' AND operation_id IS NULL AND compute_operation_id IS NULL)
            OR (kind = 'dispatch' AND compute_operation_id IS NULL)
            OR (kind = 'compute' AND operation_id IS NULL AND compute_operation_id IS NOT NULL)
        ),
    ADD CONSTRAINT runtime_executors_run_compute_operation_fkey
        FOREIGN KEY (run_id, compute_operation_id)
        REFERENCES operations(run_id, operation_id),
    ADD CONSTRAINT runtime_executors_run_generation_kind_operation_unique
        UNIQUE NULLS NOT DISTINCT (run_id, generation, kind, operation_id, compute_operation_id);

CREATE UNIQUE INDEX one_compute_executor_per_operation
    ON runtime_executors(run_id, compute_operation_id)
    WHERE kind = 'compute';

CREATE OR REPLACE FUNCTION guard_executor_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.run_id, NEW.generation, NEW.kind, NEW.operation_id, NEW.compute_operation_id,
        NEW.process_incarnation, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.run_id, OLD.generation, OLD.kind, OLD.operation_id, OLD.compute_operation_id,
        OLD.process_incarnation, OLD.created_at)
       OR (OLD.container_id IS NOT NULL AND NEW.container_id IS DISTINCT FROM OLD.container_id)
       OR (OLD.engine_id IS NOT NULL AND NEW.engine_id IS DISTINCT FROM OLD.engine_id)
       OR ((OLD.container_id IS NULL AND NEW.container_id IS NOT NULL
            OR OLD.engine_id IS NULL AND NEW.engine_id IS NOT NULL) AND OLD.state <> 'starting') THEN
        RAISE EXCEPTION 'runtime executor physical identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;

ALTER TABLE scientific_artifact_receipts
    ADD COLUMN output_index integer NOT NULL DEFAULT 0 CHECK (output_index >= 0);

ALTER TABLE scientific_artifact_receipts
    DROP CONSTRAINT scientific_artifact_receipts_pkey,
    ADD CONSTRAINT scientific_artifact_receipts_pkey PRIMARY KEY (run_id, tool_call_id, output_index);
