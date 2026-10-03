ALTER TABLE messages ADD COLUMN sequence bigint GENERATED ALWAYS AS IDENTITY;
CREATE UNIQUE INDEX messages_session_sequence ON messages(session_id, sequence);
