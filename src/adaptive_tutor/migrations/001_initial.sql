PRAGMA foreign_keys = ON;

CREATE TABLE curricula (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    description TEXT NOT NULL,
    source_path TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    loaded_at TEXT NOT NULL
) STRICT;

CREATE TABLE concepts (
    id TEXT PRIMARY KEY,
    curriculum_id TEXT NOT NULL REFERENCES curricula(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT NOT NULL,
    importance REAL NOT NULL CHECK (importance > 0),
    base_difficulty INTEGER NOT NULL CHECK (base_difficulty BETWEEN 1 AND 10),
    exercise_types_json TEXT NOT NULL,
    generation_guidance TEXT NOT NULL DEFAULT '',
    grading_guidance TEXT NOT NULL DEFAULT ''
) STRICT;

CREATE INDEX concepts_curriculum_domain_idx ON concepts(curriculum_id, domain);

CREATE TABLE concept_relationships (
    curriculum_id TEXT NOT NULL REFERENCES curricula(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    prerequisite_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL DEFAULT 'prerequisite',
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (concept_id, prerequisite_id)
) STRICT;

CREATE TABLE profiles (
    curriculum_id TEXT NOT NULL REFERENCES curricula(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    domain_weights_json TEXT NOT NULL,
    concept_weights_json TEXT NOT NULL,
    PRIMARY KEY (curriculum_id, id)
) STRICT;

CREATE TABLE assignments (
    id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    curriculum_id TEXT NOT NULL REFERENCES curricula(id),
    profile_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    exercise_type TEXT NOT NULL,
    difficulty INTEGER NOT NULL CHECK (difficulty BETWEEN 1 AND 10),
    expected_minutes INTEGER NOT NULL,
    status TEXT NOT NULL,
    branch_name TEXT,
    pull_number INTEGER,
    head_sha TEXT,
    bundle_json TEXT NOT NULL,
    current_stage INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE UNIQUE INDEX one_primary_active_assignment_idx
ON assignments(learner_id)
WHERE status IN ('validated', 'published', 'submitted', 'reviewing', 'follow_up');

CREATE TABLE assignment_concepts (
    assignment_id TEXT NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(id),
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    PRIMARY KEY (assignment_id, concept_id)
) STRICT;

CREATE TABLE assignment_stages (
    assignment_id TEXT NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    stage_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    instructions TEXT NOT NULL,
    unlock_condition TEXT NOT NULL,
    unlocked_at TEXT,
    completed_at TEXT,
    PRIMARY KEY (assignment_id, stage_number)
) STRICT;

CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    commit_sha TEXT NOT NULL,
    stage_number INTEGER NOT NULL DEFAULT 1,
    learner_confidence INTEGER CHECK (learner_confidence BETWEEN 0 AND 100),
    submission_source TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    outcome TEXT,
    failure_kind TEXT,
    UNIQUE (assignment_id, commit_sha, stage_number)
) STRICT;

CREATE TABLE automated_evaluations (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    schema_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    learner_passed INTEGER NOT NULL CHECK (learner_passed IN (0, 1)),
    artifact_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (attempt_id, artifact_digest)
) STRICT;

CREATE TABLE qualitative_evaluations (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    automated_evaluation_id TEXT REFERENCES automated_evaluations(id),
    schema_version TEXT NOT NULL,
    evaluation_json TEXT NOT NULL,
    overall_score REAL NOT NULL CHECK (overall_score BETWEEN 0 AND 100),
    grader_confidence REAL NOT NULL CHECK (grader_confidence BETWEEN 0 AND 1),
    prompt_version TEXT NOT NULL,
    supersedes_id TEXT REFERENCES qualitative_evaluations(id),
    review_kind TEXT NOT NULL DEFAULT 'initial',
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE evaluation_appeals (
    id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    original_evaluation_id TEXT NOT NULL REFERENCES qualitative_evaluations(id),
    learner_argument TEXT NOT NULL,
    status TEXT NOT NULL,
    result_evaluation_id TEXT REFERENCES qualitative_evaluations(id),
    created_at TEXT NOT NULL,
    resolved_at TEXT
) STRICT;

CREATE TABLE mastery (
    learner_id TEXT NOT NULL,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    mastery_estimate REAL NOT NULL DEFAULT 0.2 CHECK (mastery_estimate BETWEEN 0 AND 1),
    uncertainty REAL NOT NULL DEFAULT 0.8 CHECK (uncertainty BETWEEN 0 AND 1),
    evidence_count INTEGER NOT NULL DEFAULT 0,
    successful_attempts INTEGER NOT NULL DEFAULT 0,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    highest_successful_difficulty INTEGER NOT NULL DEFAULT 0,
    recent_performance REAL NOT NULL DEFAULT 0,
    long_term_performance REAL NOT NULL DEFAULT 0,
    last_reviewed TEXT,
    next_review TEXT,
    review_interval_days REAL NOT NULL DEFAULT 1,
    confidence_calibration REAL NOT NULL DEFAULT 0,
    trend REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (learner_id, concept_id)
) STRICT;

CREATE INDEX mastery_reviews_idx ON mastery(learner_id, next_review);

CREATE TABLE mastery_evidence (
    id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    assignment_id TEXT REFERENCES assignments(id),
    attempt_id TEXT REFERENCES attempts(id),
    outcome TEXT NOT NULL,
    strength REAL NOT NULL CHECK (strength BETWEEN 0 AND 1),
    difficulty INTEGER NOT NULL CHECK (difficulty BETWEEN 1 AND 10),
    exercise_type TEXT NOT NULL,
    learner_confidence INTEGER CHECK (learner_confidence BETWEEN 0 AND 100),
    transfer_context TEXT,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL
) STRICT;

CREATE INDEX mastery_evidence_concept_idx
ON mastery_evidence(learner_id, concept_id, observed_at DESC);

CREATE TABLE misconceptions (
    id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    concept_id TEXT NOT NULL REFERENCES concepts(id),
    fingerprint TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    first_observed TEXT NOT NULL,
    last_observed TEXT NOT NULL,
    frequency INTEGER NOT NULL DEFAULT 1,
    severity INTEGER NOT NULL CHECK (severity BETWEEN 1 AND 5),
    learner_confidence INTEGER CHECK (learner_confidence BETWEEN 0 AND 100),
    challenged_at TEXT,
    resolved_at TEXT,
    resolution_transfer_context TEXT,
    UNIQUE (learner_id, concept_id, fingerprint)
) STRICT;

CREATE TABLE misconception_evidence (
    id TEXT PRIMARY KEY,
    misconception_id TEXT NOT NULL REFERENCES misconceptions(id) ON DELETE CASCADE,
    assignment_id TEXT REFERENCES assignments(id),
    attempt_id TEXT REFERENCES attempts(id),
    action TEXT NOT NULL,
    evidence TEXT NOT NULL,
    exercise_type TEXT,
    transfer_context TEXT,
    observed_at TEXT NOT NULL
) STRICT;

CREATE TABLE confidence_observations (
    id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    concept_id TEXT NOT NULL REFERENCES concepts(id),
    attempt_id TEXT REFERENCES attempts(id),
    confidence INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    correctness REAL NOT NULL CHECK (correctness BETWEEN 0 AND 1),
    calibration_error REAL NOT NULL CHECK (calibration_error BETWEEN 0 AND 1),
    observed_at TEXT NOT NULL
) STRICT;

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    action TEXT,
    repository TEXT,
    delivery_id TEXT,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    received_at TEXT NOT NULL,
    processed_at TEXT,
    error TEXT,
    UNIQUE (source, delivery_id)
) STRICT;

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    deduplication_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 100,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    available_at TEXT NOT NULL,
    leased_until TEXT,
    worker_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
) STRICT;

CREATE INDEX jobs_claim_idx ON jobs(status, available_at, priority, created_at);

CREATE TABLE hints (
    id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    learner_id TEXT NOT NULL,
    level INTEGER NOT NULL CHECK (level BETWEEN 1 AND 5),
    content TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    UNIQUE (assignment_id, learner_id, level)
) STRICT;

CREATE TABLE reports (
    id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    period_type TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    report_json TEXT NOT NULL,
    markdown TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    UNIQUE (learner_id, period_type, period_start, period_end)
) STRICT;

CREATE TABLE model_invocations (
    id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    output_digest TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    failure_kind TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    error TEXT
) STRICT;

CREATE TABLE prompt_versions (
    id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    version TEXT NOT NULL,
    template_digest TEXT NOT NULL,
    template_text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (purpose, version)
) STRICT;

CREATE TABLE activity (
    id TEXT PRIMARY KEY,
    learner_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL
) STRICT;

CREATE INDEX activity_recent_idx ON activity(learner_id, occurred_at DESC);

CREATE TABLE configuration (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
