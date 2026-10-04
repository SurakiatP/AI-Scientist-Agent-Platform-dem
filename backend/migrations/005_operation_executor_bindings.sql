-- A run-wide dispatch container is lifecycle authority only. Every actual
-- outbound operation must durably bind its original generation before I/O.
ALTER TABLE operations ADD CONSTRAINT operations_generation_identity_unique
    UNIQUE (run_id, operation_id, generation);
ALTER TABLE runtime_executors ADD CONSTRAINT runtime_executor_scoped_kind_unique
    UNIQUE (id, run_id, generation, kind);

CREATE TABLE operation_executors (
    run_id uuid NOT NULL,
    operation_id varchar(200) NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    executor_id uuid NOT NULL,
    executor_kind varchar(16) NOT NULL DEFAULT 'dispatch' CHECK (executor_kind = 'dispatch'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, operation_id),
    FOREIGN KEY (run_id, operation_id, generation) REFERENCES operations(run_id, operation_id, generation),
    FOREIGN KEY (executor_id, run_id, generation, executor_kind) REFERENCES runtime_executors(id, run_id, generation, kind)
);
CREATE TRIGGER operation_executor_binding_is_immutable BEFORE UPDATE ON operation_executors
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
