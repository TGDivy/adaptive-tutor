# Evaluation

Evaluation has two independent inputs: deterministic evidence from a protected
GitHub-hosted workflow and a schema-constrained qualitative review on the tutor
host. Neither path lets untrusted repository text become trusted instructions.

## Deterministic evidence

The normalized version-1 contract identifies the assignment and learner commit,
runner, evaluator key, dispatch nonce, manifest/workflow/evaluator-kit digests,
workflow and evaluator commits, immutable repository ID, start/end times,
artifact digest, and bounded checks. The current hosted evaluator emits policy
checks plus either a public-test result or a non-executing submission-policy
result.

Each check has a bounded status, summary, duration, and typed metrics. A learner
pass requires at least one non-skipped check and every relevant check to pass.
Malformed JSON, an invalid commit, unknown status, incorrect digest, or any
provenance mismatch is rejected before persistence.

## Private bundle and public manifest

Assignment validation produces a complete `AssignmentBundle` containing public
starter/test files and tutor-only references, rubric, and evaluator guidance.
Before branch publication, the tutor seals that complete bundle into
`DATA_DIR/trusted-evaluators/spool/A-NNNN.json`. The Ed25519-signed envelope is
bound to the assignment and branch and remains in owner-only tutor state. It is
also retained in SQLite for later qualitative grading. Neither copy is sent to
GitHub Actions.

The tutor separately derives a learner-visible `PublicEvaluatorManifest` and
publishes it as `.adaptive-tutor/evaluator-manifest.json`. It contains only:

- assignment and branch identity;
- allowed submission paths and their original digests;
- learner-visible public-test paths and signed content digests;
- one fixed command identifier and bounded resource limits;
- the exact public evaluator-kit digest; and
- signing-key ID, issue time, and Ed25519 signature.

The manifest contains no private references, rubric, or evaluator guidance.
Public tests are intentionally visible in the learner repository. Their signed
digests make edits fail closed; visibility is not treated as secrecy. The
matching public key lives at `.adaptive-tutor/evaluator-signing.pub` on the
protected workspace default branch, while the private key remains on the tutor
host.

## Protected GitHub-hosted workflow

Before publication and again before dispatch, the orchestrator requires a
provisioned evaluator control-plane record. It verifies the private workspace's
immutable repository ID, protected default-branch workflow digest, and public
key ID against that record. A learner push then receives a unique dispatch
nonce and is dispatched with the exact assignment, branch, learner commit,
manifest digest, public evaluator source commit, and evaluator-kit digest.

The protected `adaptive-tutor-evaluate.yml` job runs on GitHub-hosted
`ubuntu-24.04` with read-only contents permission and SHA-pinned actions. It:

1. checks out the protected workflow and public key at `github.workflow_sha`;
2. checks out the public evaluator source at the exact `evaluator_ref`;
3. checks out the exact learner commit into a separate directory;
4. recomputes the evaluator-kit digest before installing its locked runtime;
5. installs Bubblewrap and invokes `adaptive_tutor.public_evaluator` under
   `env -i`; and
6. uploads only the normalized `adaptive-tutor-evidence.json` artifact.

Every checkout uses `persist-credentials: false`. No tutor-host bundle, signing
key, GitHub App key, model credential, or dashboard token enters the job.

## Public evaluator isolation

Only manifest-declared starter files enter the submission tree. Public tests
are read from the learner checkout only after their bytes match the signed
manifest; a missing, substituted, oversized, or symlinked file is rejected.
A trusted supervisor accepts success only after every collected public test
reports passing and a nonce-bound completion record arrives over a descriptor
that test workers never inherit. `os._exit(0)`, a worker crash, missing tests,
skipped tests, or a forged process code therefore cannot manufacture a pass.

Bubblewrap gives the test group a read-only filesystem, private user/PID/IPC/UTS
namespaces, no procfs, no network, a minimal device tree, and a private writable
tmpfs. The supervisor also strips credential-like environment variables,
applies CPU/output/address-space/process/descriptor limits, and kills the whole
process group after every outcome. Raw pytest and learner output remains in a
bounded temporary file and never enters normalized evidence; the artifact
contains only aggregate counts and fixed policy text. The artifact is written
atomically outside the sandbox with mode `0600` and a canonical SHA-256 digest.

## Provenance acceptance

The tutor accepts a workflow run only when its workflow ID/path, repository and
head repository, `workflow_dispatch` event, default branch, typed run title,
workflow commit, workflow digest, and repository ID match protected state. It
then compares the artifact's nonce, manifest/workflow/evaluator-kit digests,
workflow/evaluator commits, repository ID, key ID, assignment, and learner
commit with the stored attempt. Any mismatch fails before qualitative review or
learner-state mutation.

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
files, private rubric content, and tutor-only evaluator guidance are not posted
to the learner pull request.

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
