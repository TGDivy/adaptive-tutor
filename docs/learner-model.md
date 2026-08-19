# Learner model

The learner model is a durable evidence ledger plus current projections. It is
not conversation memory, and it is never reconstructed from a model transcript.

## Per-concept state

For every learner and concept, SQLite retains:

- mastery estimate and uncertainty;
- total evidence, successful attempts, and failed attempts;
- highest successfully demonstrated difficulty;
- recent and long-term performance;
- last review, next review, and interval length;
- confidence calibration; and
- trend.

The dashboard and scheduler read this compact projection. Every underlying
observation remains in `mastery_evidence`, including the assignment, attempt,
outcome, strength, difficulty, exercise type, confidence, transfer context,
source, timestamp, and mastery before/after transition.

## Updating from evidence

Only a validated `QualitativeEvaluation` can update the model. The update:

1. checks that the evaluation has not already been applied;
2. converts concept evidence into a bounded weighted outcome;
3. moves mastery and uncertainty without leaving `[0, 1]`;
4. updates recent and long-term performance at different rates;
5. adjusts retrieval spacing;
6. appends confidence and misconception evidence; and
7. records activity—all in one transaction.

Duplicate delivery of the same attempt/evaluation is idempotent. If any write
fails, the transaction rolls back rather than leaving half an update.

## Confidence calibration

Assignments can request confidence from 0–100. The model stores confidence,
observed correctness, and absolute calibration error. A confident failure has
two consequences: the concept returns sooner and the calibration view becomes
more cautious. A low-confidence success is also useful evidence—it may indicate
knowledge that has not yet become reliably accessible.

Calibration is a scheduling signal, not a character judgment. Missing
confidence does not fabricate a value.

## Readiness

Domain readiness is the importance-weighted mean of current concept mastery.
Domain uncertainty is calculated the same way. Reports present both because a
high estimate from sparse evidence should not look as settled as repeated
transfer success.

Profiles affect what the scheduler prioritizes; they do not rewrite historical
mastery. Switching profiles changes emphasis without erasing evidence.

## Misconceptions

Misconceptions have their own evidence stream, frequency, severity, confidence,
and lifecycle. They are linked to concepts but are not collapsed into one
negative mastery number. This separation lets the system challenge a specific
model of the problem while preserving unrelated demonstrated skill.

Resolution requires successful transfer in a different exercise type with a
new context. A future recurrence remains visible rather than silently replacing
the prior resolution.

## Reports and auditability

Weekly and monthly reports derive study activity, mastery movement, retention,
calibration, misconceptions, difficulty, readiness, model usage, and
recommended focus from SQLite. Mastery movement sums recorded before/after
transitions; it is not inferred from today's state.

Use `adaptive-tutor concepts --json`, `status --json`, and `report --format
json` for personal analysis. Treat raw SQLite as private learner data and keep
it out of source control and public support requests.
