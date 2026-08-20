CREATE TABLE learning_goals (
    id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    curriculum_id TEXT NOT NULL REFERENCES curricula(id) ON DELETE CASCADE,
    profile_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    statement TEXT NOT NULL CHECK (length(statement) BETWEEN 1 AND 2000),
    target_date TEXT,
    focus_domains_json TEXT NOT NULL DEFAULT '[]',
    focus_concepts_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    created_at TEXT NOT NULL,
    superseded_at TEXT,
    UNIQUE (learner_id, curriculum_id, revision),
    CHECK (
        (status = 'active' AND superseded_at IS NULL)
        OR (status = 'superseded' AND superseded_at IS NOT NULL)
    )
) STRICT;

CREATE UNIQUE INDEX one_active_learning_goal_idx
ON learning_goals(learner_id, curriculum_id)
WHERE status = 'active';

CREATE INDEX learning_goal_history_idx
ON learning_goals(learner_id, curriculum_id, revision DESC);
