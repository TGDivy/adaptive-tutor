<div align="center">

# Adaptive Tutor

**A self-hosted, Git-native adaptive learning engine.**

Turn technical practice into a continuous workflow of focused assignments,
deterministic evidence, structured review, spaced retrieval, and visible progress.

[Documentation](https://tgdivy.github.io/adaptive-tutor/) ·
[Quick start](#quick-start) ·
[Security](#security-model)

</div>

> Adaptive Tutor is under active construction. The repository is being built
> against the complete contract in [SPEC.md](SPEC.md); tagged releases begin
> after the independent completion gate passes.

## What it feels like

You work with the tools you already use—an editor, terminal, Git, and pull
requests. Adaptive Tutor chooses the next useful challenge, opens an assignment
branch, consumes credential-free CI evidence, requests a schema-validated Codex
review, updates a durable learner model, and schedules retrieval or a follow-up.

The engine is curriculum-neutral. Curriculum packages supply concepts,
prerequisites, profiles, references, and grading guidance without changing core
code. A neutral systems-foundations curriculum and a credential-free local demo
ship with the project.

## Quick start

The final installation flow is:

```bash
pipx install adaptive-tutor
adaptive-tutor init
adaptive-tutor doctor
adaptive-tutor demo
```

During source development:

```bash
git clone https://github.com/tgdivy/adaptive-tutor.git
cd adaptive-tutor
uv sync --extra dev
uv run adaptive-tutor demo
```

## Architecture

```text
GitHub webhook ──► durable event/job queue ──► evaluator orchestration
                                                        │
credential-free Actions ──► normalized evidence ────────┤
                                                        ▼
                                                bounded Codex worker
                                                        │
                                                        ▼
                         SQLite learner model ◄── validated transaction
                                   │
                          CLI / private dashboard / reports
```

The webhook request only authenticates, persists, and enqueues. Learner code
runs in ephemeral CI without tutor credentials. Qualitative review runs as a
short-lived, read-only worker; SQLite—not model history—is the system of record.

## Security model

- Webhook signatures are verified before payloads are accepted.
- Duplicate deliveries are idempotent.
- Untrusted code never receives model, repository-write, dashboard, or agent
  credentials.
- Repository and learner text is delimited as untrusted data for model review.
- The dashboard binds to loopback by default and requires authorization when
  exposed through a private network.
- Public-boundary checks reject credentials, internal infrastructure references,
  learner data, and private curriculum material before release.

## Project status

The implementation ledger lives in
[`implementation/completion.json`](implementation/completion.json), and the
authoritative gate is:

```bash
./scripts/check-completion
```

It intentionally fails until product, operations, documentation, external
integration, and security evidence are all present. See [SPEC.md](SPEC.md) for
the full public contract.

## License

[MIT](LICENSE)
