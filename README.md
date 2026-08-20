<div align="center">

# Adaptive Tutor

**A self-hosted, Git-native adaptive learning engine.**

Turn technical practice into a continuous workflow of focused assignments,
deterministic evidence, structured review, spaced retrieval, and visible progress.

[Documentation](https://tgdivy.github.io/adaptive-tutor/) ·
[Quick start](#quick-start) ·
[Security](#security-model)

[![CI](https://github.com/TGDivy/adaptive-tutor/actions/workflows/ci.yml/badge.svg)](https://github.com/TGDivy/adaptive-tutor/actions/workflows/ci.yml)
[![Security](https://github.com/TGDivy/adaptive-tutor/actions/workflows/security.yml/badge.svg)](https://github.com/TGDivy/adaptive-tutor/actions/workflows/security.yml)
[![Docs](https://github.com/TGDivy/adaptive-tutor/actions/workflows/docs.yml/badge.svg)](https://tgdivy.github.io/adaptive-tutor/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

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

Run the full credential-free learning loop from a source checkout:

```bash
git clone https://github.com/tgdivy/adaptive-tutor.git
cd adaptive-tutor
uv sync --locked --extra dev
uv run adaptive-tutor demo
```

To keep private local state and inspect recommendations:

```bash
uv run adaptive-tutor init
uv run adaptive-tutor doctor --offline
uv run adaptive-tutor next --dry-run --available-minutes 30
uv run adaptive-tutor status
```

The demo uses no credentials or network calls. It loads curriculum data,
selects and validates assignments, executes product-owned passing and failing
fixture submissions in a scrubbed process, applies schema-valid fixture reviews
transactionally, and generates a progress report.
See the [installation guide](https://tgdivy.github.io/adaptive-tutor/getting-started/)
for isolated installs, dashboard startup, and remote integration.

## Architecture

```text
GitHub webhook ──► durable event/job queue ──► evaluator orchestration
                                                        │
GitHub-hosted Actions ──► normalized evidence ──────────┤
                                                        ▼
                                                durable state worker
                                                        │ Unix socket
                                                        ▼
                                                isolated Codex grader
                                                        │
                                                        ▼
                         SQLite learner model ◄── validated transaction
                                   │
                          CLI / private dashboard / reports
```

The webhook request only authenticates, persists, and enqueues. Learner code
runs in ephemeral CI without tutor credentials. Qualitative review runs as a
short-lived, read-only process in a service that cannot see tutor state or
GitHub credentials; SQLite—not model history—is the system of record.

## Security model

- Webhook signatures are verified before payloads are accepted.
- Duplicate deliveries are idempotent.
- Untrusted code never receives model, repository-write, dashboard, or agent
  credentials.
- Each learner branch carries an Ed25519-signed public evaluator manifest that
  binds the assignment, branch, allowed files, visible test digests, fixed
  command and limits, and exact evaluator-kit digest.
- Private references, rubrics, and evaluator guidance remain in an owner-only
  tutor-host bundle and never enter GitHub Actions.
- Repository and learner text is delimited as untrusted data for model review.
- The dashboard binds to loopback and requires authorization by default;
  exposed binds refuse to start without a token.
- Public-boundary checks reject credentials, internal infrastructure references,
  learner data, and private curriculum material before release.

Read the complete [security model](https://tgdivy.github.io/adaptive-tutor/security/)
before enabling GitHub or model credentials.

## Deploy

Hardened Docker Compose and systemd paths include non-root/read-only services,
a grader-only model credential, health checks, restart recovery, daily online
backups, upgrade/rollback, and disaster recovery procedures.

```bash
cd deploy
./prepare-compose.sh
docker compose build
docker compose --profile tools run --rm initializer
docker compose up -d tutor
```

Follow the [operations guide](https://tgdivy.github.io/adaptive-tutor/operations/)
rather than exposing the example service directly.

The local runtime deployment paths are implemented. Automated bootstrap of the
protected GitHub evaluator controls is not yet supported, so the remote GitHub
learning loop is not install-ready in this construction build.

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
