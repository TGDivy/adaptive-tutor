# Evaluation

Evaluation has two independent inputs: deterministic evidence from ephemeral CI
and a schema-constrained qualitative review. Neither is allowed to smuggle
untrusted text into trusted instructions.

## Deterministic evidence

The normalized version-1 contract identifies the assignment and commit, runner,
verified evaluator binding/key identity, start/end times, artifact digest, and a
list of checks. The worker compares that evaluator identity with its signed
spool before persisting evidence. Check categories include:

- compile, test, integration, and stress;
- sanitizer and static analysis;
- benchmark, output, and allocation evidence; and
- policy checks.

Each check has a bounded status, summary, duration, and typed metrics. A learner
pass requires at least one non-skipped check and every relevant check to pass.
Malformed JSON, an invalid commit, unknown status, or incorrect artifact digest
is rejected before persistence.

## Trusted bundle provisioning

Assignment validation produces public files plus private references, hidden
tests, and evaluator guidance. Before any branch or pull request is created, the
worker seals the complete bundle in
`DATA_DIR/trusted-evaluators/spool/A-NNNN.json`. The envelope binds the bundle to
the assignment ID and exact branch, records canonical bundle and binding
digests, and carries an Ed25519 signature. Its signing key, spool
directory, and envelope files are owner-only (`0600` files inside `0700`
directories).

An ephemeral-runner provisioner must stage the envelope before registering the
one-job runner:

```bash
adaptive-tutor --config /etc/adaptive-tutor/config.yaml stage-evaluator A-0001 \
  --run-id 123456789 \
  --branch assignment/0001-bounded-work-queue \
  --commit-sha 0123456789abcdef0123456789abcdef01234567 \
  --output /runner/temp/trusted/assignment-bundle.json \
  --verification-key-output /runner/temp/trusted/evaluator-signing.pub
```

`stage-evaluator` verifies that the run ID belongs to the protected
default-branch workflow and its typed identity exactly matches canonical SQLite
state. It creates the signed spool record when derived spool state is absent,
verifies the signature and both digests against the database bundle, then
issues a short-lived runner envelope bound to the exact commit. The
envelope and Ed25519 public key are written atomically with mode `0600`; the
private key never leaves tutor state. Transfer the staged files only through the
trusted provisioner channel; do not upload them as repository or Actions
artifacts.

## Credential-free evaluator command

The workspace workflow invokes the same hidden command used by integration
tests:

```bash
env -i PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
  adaptive-tutor evaluate \
  --bundle /runner/trusted/assignment-bundle.json \
  --verification-key /runner/trusted/evaluator-signing.pub \
  --workspace /runner/learner-checkout \
  --output /runner/evidence/adaptive-tutor-evidence.json \
  --assignment-id A-0001 \
  --branch assignment/0001-bounded-work-queue \
  --commit-sha 0123456789abcdef0123456789abcdef01234567
```

The trusted envelope, verification key, and evidence destination must be
outside the untrusted checkout. The evaluator requires owner-only, non-symlink
files; authenticates the Ed25519 signature; enforces the short validity window;
recomputes both digests; and matches the assignment, branch, commit, and public
binding. It consumes the staged envelope and key before learner code starts.

Only declared starter files enter the submission tree. Public tests are copied
from the signed bundle rather than the learner checkout, and hidden tests come
from the same authenticated bundle. A trusted xdist controller runs outside the
learner test process and accepts success only after every collected test reports
passing and a nonce-bound completion record arrives over a descriptor that test
workers never inherit. `os._exit(0)`, a worker crash, missing tests, skipped
tests, or a forged process code therefore cannot manufacture a pass.

Bubblewrap gives the test group a read-only filesystem, private user/PID/IPC/UTS
namespaces, no procfs, no network, a minimal device tree, and a private writable
tmpfs. The supervisor also strips credential-like environment variables,
applies CPU/output/address-space/process/descriptor limits, and kills the whole
process group after every outcome. Raw pytest and learner output remains in a
bounded temporary file and never enters normalized evidence; the artifact
contains only aggregate counts and fixed policy text. The artifact is written
atomically outside the sandbox with mode `0600` and a canonical SHA-256 digest.

## Trust-separated review prompt

The review builder emits visibly delimited sections for:

1. trusted grading instructions;
2. trusted assignment and current-stage context;
3. trusted rubric;
4. trusted references;
5. normalized CI evidence;
6. learner context; and
7. untrusted submission text.

The assignment context is reconstructed from persisted tutor state, not from
the repository. It includes the exact authored stage instructions, unlock
condition, stage count, target concepts, and reference expectations. A passing
review cannot skip an authored follow-up stage, and a final-stage review cannot
request a stage that does not exist. Every stage remains a distinct attempt in
the same pull request.

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
