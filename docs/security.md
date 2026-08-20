# Security

Adaptive Tutor processes hostile repository text and code while holding access
to private learner state, GitHub, and a model. Its primary security property is
separation: untrusted execution never shares a credentialed process boundary.

## Assets

- private curriculum intent and trusted references;
- learner submissions, history, misconceptions, and reports;
- dashboard/personal-agent token and webhook secret;
- GitHub App private key or development token;
- evaluator-manifest signing key and protected verification-key trust anchor;
- model API key or Codex account state; and
- the integrity of grading and learner-state evidence.

## Trust boundaries

| Input/process | Trust | Allowed capability |
| --- | --- | --- |
| Curriculum package | Operator-trusted, schema-validated | Supply concepts, guidance, references, fixtures. |
| Webhook request | Authenticated but untrusted payload | Persist and enqueue only after HMAC and repository scope checks. |
| Learner repository/content | Untrusted | Execute only in credential-free GitHub-hosted CI. |
| Private tutor-host bundle | Tutor-trusted, owner-only | Retain references, rubric, and evaluator guidance for qualitative grading; never enter CI. |
| Public evaluator manifest | Learner-visible, signature-verified | Bind public files, command, limits, evaluator kit, assignment, and branch. |
| CI artifact | Untrusted until contract/digest validation | Become deterministic evidence, never instructions. |
| Codex output | Untrusted until schema validation | Become qualitative evidence only after validation. |
| Tutor/worker | Trusted | GitHub orchestration and transactional state updates. |
| Isolated grader | Trusted but credential-minimal | Receive one bounded prompt and return one schema-valid review. |

## Untrusted execution

Never run a learner checkout, pull request script, build system, or arbitrary
test command on the persistent tutor host. The private workspace workflow runs
on GitHub-hosted `ubuntu-24.04`; learner code executes under `env -i` inside a
Bubblewrap namespace with no network or host procfs, a read-only evaluation
tree, private temporary storage, bounded resources, and process-tree cleanup.

Public tests are learner-visible. Their signed content digests are
authoritative, so an edit, deletion, substitution, symlink, or oversized file
fails before execution. Learner imports execute only in isolated test workers,
while a separate trusted supervisor requires a nonce-bound completion record
and a complete set of passing reports. Raw test output is quarantined and never
copied into the evidence artifact, preventing terminal control data or
repository text from reaching later grading stages.

Checkout actions must not persist credentials. Job permissions must be
read-only. Do not reference secrets in a job that later invokes learner code,
and never use an untrusted workflow revision with `pull_request_target`.

## Prompt injection

Learner text, repository files, compiler logs, CI output, and appeal arguments
are delimited untrusted data. They cannot change grading instructions, request
tools, reveal secrets, or override the output schema. Injection-like patterns
are surfaced as flags, not followed.

The stateful worker cannot launch Codex directly. It talks over a group-scoped
Unix socket to a grader running under a distinct UID. The model environment is
root-owned outside tutor-controlled configuration, and Codex state belongs only
to the grader UID. The shared socket group grants connection access to worker
and grader units but no write access to the runtime directory; tutor and backup
receive no such group. The grader has no tutor state, configuration, GitHub
credential, learner checkout, or TCP listener. The Codex subprocess uses a
read-only sandbox, no approvals, an ephemeral session, and an empty working
directory containing only the schema/output contract.

## Web and API

- Loopback bind is the default.
- Dashboard and read API authentication are on by default.
- Writes require a bearer token, not only a session cookie.
- Login cookies are HTTP-only, strict same-site, and secure when configured for
  a non-loopback bind.
- CSP forbids scripts and framing; responses are not cached.
- Exposed binds fail startup without an API token.
- Health probes reveal only process/readiness state.

## Storage and secrets

Config and secret files are mode `0600`; state directories are `0700`. SQLite
uses mode `0600`, foreign keys, integrity checks, versioned migrations, and
online backups. Keep backups encrypted and off-host.

Configuration stores secret *references*. Put raw values only in owner-only
environment/secret files. Never commit `.env`, private keys, SQLite, artifacts,
screenshots with tokens, or generated private curricula.

Complete assignment bundles are Ed25519-signed into an owner-only tutor-host
spool before GitHub publication. They contain private references, rubric, and
evaluator guidance and never enter the learner repository, an Actions cache, or
an artifact. A separate `PublicEvaluatorManifest` is published with only the
assignment and branch, allowed submission/public-test paths and digests, fixed
command and limits, evaluator-kit digest, key ID, issue time, and signature.

The protected default branch supplies the matching public key and workflow.
Before publication/dispatch, the tutor compares their key ID, workflow digest,
and immutable repository ID with provisioned control state. The hosted job
checks out controls at `github.workflow_sha`, evaluator source at an exact
commit, and the learner commit separately, then recomputes the kit digest. On
ingest, the tutor binds the run and artifact back to the stored nonce, manifest,
workflow, evaluator source, repository, assignment, and learner commit.

Back up `trusted-evaluators/signing.key` in encrypted owner-only storage with
SQLite; it cannot be derived from the database. Restore the matching key after
loss. Without it, retire the affected workspace/state and perform a fresh
guided installation; silently generating a replacement does not authenticate
already published manifests or match the protected workspace key.

The public-boundary gate scans every tracked file for private subject material,
organization infrastructure, key blocks, and token formats. It complements—
but does not replace—repository secret scanning and human review.

## GitHub scope

Use a dedicated App installed only on the single private learning workspace.
Private curriculum checkouts remain operator-managed and outside App scope.
Verify the workspace is private before publishing. Branch paths, artifact zip
entries, sizes, and evidence contracts are validated. Public pull requests must
never be routed to a credentialed evaluator.

## Failure behavior

Security, schema, model, generator, assignment, dependency, and infrastructure
failures do not lower mastery. Retryable work uses bounded exponential backoff;
non-retryable or exhausted jobs preserve redacted dead-letter diagnostics.
Original appeal reviews remain immutable.

## Reporting a vulnerability

Do not open a public issue containing exploit details, learner data, repository
names, or secrets. Use GitHub's private vulnerability reporting for the public
repository. Rotate affected credentials first, preserve bounded audit evidence,
and follow [disaster recovery](operations.md#failure-recovery) when integrity is
uncertain.
