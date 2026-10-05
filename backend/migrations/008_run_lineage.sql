ALTER TABLE runs ADD COLUMN retry_of uuid;
ALTER TABLE runs ADD CONSTRAINT runs_retry_of_project_fk
    FOREIGN KEY (retry_of, project_id) REFERENCES runs(id, project_id);
ALTER TABLE runs ADD CONSTRAINT runs_retry_not_self CHECK (retry_of IS NULL OR retry_of <> id);
ALTER TABLE messages ADD COLUMN run_id uuid;
ALTER TABLE messages ADD CONSTRAINT messages_run_project_fk
    FOREIGN KEY (run_id, project_id) REFERENCES runs(id, project_id);
CREATE UNIQUE INDEX messages_run_question ON messages(run_id) WHERE run_id IS NOT NULL AND role = 'user';
