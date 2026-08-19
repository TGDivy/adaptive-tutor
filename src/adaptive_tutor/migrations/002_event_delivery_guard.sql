CREATE UNIQUE INDEX IF NOT EXISTS events_payload_fallback_idx
ON events(source, event_type, payload_digest)
WHERE delivery_id IS NULL;

CREATE INDEX IF NOT EXISTS qualitative_attempt_idx
ON qualitative_evaluations(attempt_id, created_at DESC);

CREATE INDEX IF NOT EXISTS misconceptions_active_idx
ON misconceptions(learner_id, status, severity DESC);
