CREATE UNIQUE INDEX messages_run_assistant
    ON messages (run_id)
    WHERE run_id IS NOT NULL AND role = 'assistant';
