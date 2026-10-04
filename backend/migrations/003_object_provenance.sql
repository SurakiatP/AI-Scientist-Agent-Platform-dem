-- Logical file versions retain extraction/publication provenance independently of S3.
ALTER TABLE file_versions ADD CONSTRAINT file_versions_id_project_unique UNIQUE (id, project_id);
ALTER TABLE file_versions ADD COLUMN extracted_artifact_id uuid;
ALTER TABLE file_versions ADD COLUMN source_artifact_id uuid;
ALTER TABLE file_versions ADD COLUMN tombstoned_at timestamptz;
ALTER TABLE file_versions ADD CONSTRAINT file_extraction_project_fk
    FOREIGN KEY (extracted_artifact_id, project_id) REFERENCES artifacts(id, project_id);
ALTER TABLE file_versions ADD CONSTRAINT file_publication_project_fk
    FOREIGN KEY (source_artifact_id, project_id) REFERENCES artifacts(id, project_id);

CREATE TABLE stored_objects (
    key text PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size bigint NOT NULL CHECK (size >= 0),
    content_type varchar(255) NOT NULL CHECK (length(content_type) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (key, project_id),
    CHECK (key = project_id::text || '/' || sha256)
);
CREATE TRIGGER stored_object_metadata_is_immutable BEFORE UPDATE ON stored_objects
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_record_change();
