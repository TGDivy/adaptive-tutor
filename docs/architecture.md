# Architecture

Adaptive Tutor separates event ingestion, untrusted execution, qualitative
review, and canonical learner state. The design is intentionally boring at the
center: versioned SQLite transactions and explicit JSON contracts.

```text
                                 private GitHub workspace
                               ┌──────────────────────────┐
learner/editor ── push/PR ─────► assignment branch + CI  │
                               │ credential-free runner   │
                               └────────────┬─────────────┘
                                            │ normalized artifact
                                            ▼
GitHub webhook ── HMAC verify ──► events ──► durable jobs
                                            │ lease / retry / dead letter
                                            ▼
                                     trusted orchestrator
                                      │             │
                           GitHub reads/writes       │ delimited prompt
                                      │             ▼
                                      │     short-lived Codex worker
                                      │       read-only + schema
                                      └──────┬──────┘
                                             ▼
                                   one SQLite transaction
                                             │
                              CLI / dashboard / API / reports
```

## Components

### Curriculum loader

Validates metadata, concepts, references, prompts, profiles, fixtures, and an
acyclic prerequisite graph. A content digest and prompt versions make the
loaded source auditable.

### Scheduler

Reads a scoped learner-state projection and returns explainable candidates. It
does not generate assignments or call a model.

### Assignment service

Generates a bounded bundle, validates information sufficiency and trusted
reference behavior, records hidden and public roles separately, and publishes
only safe files.

### Event store and worker

The request path verifies the webhook signature, bounds the payload, stores a
delivery exactly once, enqueues work, and responds. Workers lease durable jobs,
renew through retries, and retain classified failure diagnostics.

### Evaluator

Deterministic evidence and untrusted submission content stay separate from
trusted instructions, rubric, and references. The Codex process is ephemeral;
its final message must satisfy the exact Pydantic-derived JSON Schema.

### Learner model

Only valid structured review reaches one transaction that appends historical
evidence and updates current projections. SQLite remains authoritative after
process restarts or model changes.

## State and concurrency

SQLite uses foreign keys, write-ahead logging, a busy timeout, immediate write
transactions, integrity checks, and numbered migrations. Durable event and job
rows recover across process crashes. Assignment and evaluation idempotency keys
prevent duplicate webhook delivery from double-counting learning evidence.

The product is deliberately single-learner today, but learner identifiers are
present throughout the storage model. Do not expose one instance as an
unreviewed multi-tenant service: authentication and operator assumptions are
for a private self-hosted tutor.

## Deployment boundaries

- The persistent host runs the dashboard, event worker, SQLite, and read-only
  Codex subprocesses.
- Learner code runs only in credential-free ephemeral CI.
- GitHub-write and model credentials are available only to trusted integration
  processes and are filtered before child execution.
- Public product source contains no private curriculum or learner repository.

See [Security](security.md) for threat assumptions and
[Operations](operations.md) for concrete hardening.
