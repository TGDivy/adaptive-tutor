# Evaluation

Evaluation has two independent inputs: deterministic evidence from ephemeral CI
and a schema-constrained qualitative review. Neither is allowed to smuggle
untrusted text into trusted instructions.

## Deterministic evidence

The normalized version-1 contract identifies the assignment and commit, runner,
start/end times, artifact digest, and a list of checks. Check categories include:

- compile, test, integration, and stress;
- sanitizer and static analysis;
- benchmark, output, and allocation evidence; and
- policy checks.

Each check has a bounded status, summary, duration, and typed metrics. A learner
pass requires at least one non-skipped check and every relevant check to pass.
Malformed JSON, an invalid commit, unknown status, or incorrect artifact digest
is rejected before persistence.

## Trust-separated review prompt

The review builder emits visibly delimited sections for:

1. trusted grading instructions;
2. trusted rubric;
3. trusted references;
4. normalized CI evidence;
5. learner context; and
6. untrusted submission text.

Submission content is data even when it says “ignore previous instructions,”
imitates system messages, or asks for secrets. Safety heuristics record likely
injection patterns for the review without executing or obeying them.

## Qualitative schema

The Codex worker must return:

- overall score and grader confidence;
- at least three unique dimension scores with rationales;
- per-concept outcome, strength, difficulty, format, rationale, and optional
  transfer context;
- misconception actions and evidence;
- concise and detailed feedback;
- a classification, follow-up action, reason, and escalation signal.

Classifications distinguish wrong, incomplete, weakly justified correct work,
valid alternatives, style preferences, performance trade-offs, and correct
work. That prevents a stylistic disagreement from becoming a fabricated
knowledge failure.

Pydantic generates the JSON Schema passed to `codex exec`. The returned final
message is validated again in-process. Timeout, process failure, missing output,
or invalid schema records a model failure and cannot update learner state.

## Review publication

Rendered review text contains the score, classification, confidence,
dimensions, feedback, next action, and an evaluation digest. Trusted reference
files and hidden evaluator details are not posted to the learner pull request.

## Appeals

An appeal must identify an existing assignment evaluation and include a
non-empty argument. The original evaluation is retained. A new independent
review receives the original as evidence—not as a conclusion—and appends its
own schema-valid result linked by `supersedes_id`.

The appeal path does not rewrite the original review or silently mutate prior
evidence. Human review remains available when confidence or ambiguity warrants
escalation.

## Failure attribution

Only learner evidence may reduce mastery. Infrastructure, assignment,
generator, model, schema, security, and dependency failures are classified and
retried or dead-lettered; they never masquerade as an incorrect solution.
