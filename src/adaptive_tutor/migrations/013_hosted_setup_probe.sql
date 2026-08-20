CREATE TABLE hosted_setup_probes (
    id TEXT PRIMARY KEY,
    setup_run_id TEXT NOT NULL REFERENCES setup_runs(id) ON DELETE CASCADE,
    repository_id INTEGER NOT NULL,
    nonce TEXT NOT NULL UNIQUE CHECK (length(nonce) = 32),
    actions_run_id INTEGER UNIQUE,
    status TEXT NOT NULL CHECK (
        status IN ('dispatching', 'dispatched', 'passed', 'failed')
    ),
    workflow_path TEXT NOT NULL,
    workflow_digest TEXT NOT NULL,
    workflow_commit TEXT NOT NULL,
    evaluator_key_id TEXT NOT NULL,
    artifact_digest TEXT,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE INDEX hosted_setup_probes_run_idx
ON hosted_setup_probes(setup_run_id, created_at);
