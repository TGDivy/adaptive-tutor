CREATE TABLE github_app_setup_sessions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES setup_runs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL CHECK (phase IN ('manifest', 'installation')),
    status TEXT NOT NULL CHECK (status IN ('active', 'complete', 'cancelled')),
    state_digest TEXT NOT NULL UNIQUE,
    app_id INTEGER,
    app_slug TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE UNIQUE INDEX one_active_github_app_setup_idx
ON github_app_setup_sessions((1))
WHERE status = 'active';

CREATE INDEX github_app_setup_run_idx
ON github_app_setup_sessions(run_id, status, created_at);
