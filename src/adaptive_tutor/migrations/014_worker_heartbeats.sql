CREATE TABLE worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('running', 'stopped')),
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    stopped_at TEXT
) STRICT;

CREATE INDEX worker_heartbeats_health_idx
ON worker_heartbeats(status, heartbeat_at DESC);

INSERT INTO setup_steps(
    run_id, name, position, status, detail, updated_at
)
SELECT id, 'worker_health', 11, 'pending',
       'Persistent worker health must be verified after upgrade', updated_at
FROM setup_runs
WHERE NOT EXISTS (
    SELECT 1 FROM setup_steps
    WHERE setup_steps.run_id = setup_runs.id
      AND setup_steps.name = 'worker_health'
);
