ALTER TABLE assignments ADD COLUMN evaluator_manifest_json TEXT;
ALTER TABLE assignments ADD COLUMN evaluator_manifest_digest TEXT;
ALTER TABLE assignments ADD COLUMN evaluator_key_id TEXT;

ALTER TABLE attempts ADD COLUMN dispatch_nonce TEXT;
ALTER TABLE attempts ADD COLUMN manifest_digest TEXT;
ALTER TABLE attempts ADD COLUMN workflow_digest TEXT;
ALTER TABLE attempts ADD COLUMN workflow_commit TEXT;
ALTER TABLE attempts ADD COLUMN evaluator_ref TEXT;
ALTER TABLE attempts ADD COLUMN evaluator_kit_digest TEXT;
ALTER TABLE attempts ADD COLUMN repository_id INTEGER;

CREATE UNIQUE INDEX attempts_dispatch_nonce_idx
ON attempts(dispatch_nonce)
WHERE dispatch_nonce IS NOT NULL;

CREATE TABLE evaluator_control_planes (
    repository_id INTEGER PRIMARY KEY,
    repository_full_name TEXT NOT NULL UNIQUE,
    default_branch TEXT NOT NULL,
    workflow_path TEXT NOT NULL,
    workflow_commit TEXT NOT NULL,
    workflow_digest TEXT NOT NULL,
    evaluator_ref TEXT NOT NULL,
    evaluator_kit_digest TEXT NOT NULL,
    evaluator_key_id TEXT NOT NULL,
    configured_at TEXT NOT NULL,
    verified_at TEXT NOT NULL
) STRICT;
