CREATE TABLE setup_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (
        status IN ('provisioning', 'action_required', 'failed', 'ready')
    ),
    public_url TEXT NOT NULL,
    goal_statement TEXT NOT NULL CHECK (length(goal_statement) BETWEEN 1 AND 2000),
    config_path TEXT NOT NULL,
    learner_id TEXT NOT NULL,
    curriculum_id TEXT NOT NULL REFERENCES curricula(id),
    goal_id TEXT REFERENCES learning_goals(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE UNIQUE INDEX one_unfinished_setup_run_idx
ON setup_runs((1))
WHERE status != 'ready';

CREATE TABLE setup_steps (
    run_id TEXT NOT NULL REFERENCES setup_runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position > 0),
    status TEXT NOT NULL CHECK (
        status IN (
            'pending', 'running', 'waiting_user', 'failed_retryable',
            'failed_terminal', 'complete'
        )
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    detail TEXT NOT NULL DEFAULT '',
    action TEXT,
    external_ids_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, name),
    UNIQUE (run_id, position)
) STRICT;

CREATE INDEX setup_steps_status_idx ON setup_steps(run_id, status, position);
