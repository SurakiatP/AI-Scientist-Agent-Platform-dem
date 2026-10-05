-- Additive uniqueness: fail migration if prior receipts conflict; retain every row.
ALTER TABLE peer_outbound_receipts
    ADD CONSTRAINT peer_outbound_receipts_run_release_unique UNIQUE (run_id, release_id);

ALTER TABLE peer_outbound_receipts
    ADD CONSTRAINT peer_outbound_receipts_peer_message_unique UNIQUE (peer_id, message_id);
