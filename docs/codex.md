# Codex grader

Adaptive Tutor uses Codex for one bounded qualitative review at a time. Codex
is not the scheduler, state store, or authority for learner evidence.

## Isolation boundary

The stateful worker never launches Codex. It sends a bounded trusted prompt to
a group-scoped Unix socket and receives a schema-valid response. A separate
`adaptive-tutor-grader` UID owns that socket and is the only identity given
model authentication or a writable `CODEX_HOME`.

The grader has no mount for:

- the tutor configuration or SQLite database;
- GitHub App keys or development tokens;
- learner repositories or CI artifacts; or
- dashboard and webhook secrets.

Compose enforces this with separate mounts and environment files. The systemd
grader uses a private mount namespace with `/var/lib/adaptive-tutor` and
`/etc/adaptive-tutor` inaccessible. Its root-owned environment file is outside
the state account's writable tree. The grader pre-binds a `0660` socket owned by
`adaptive-tutor-grader:adaptive-tutor-grader-socket` inside a `0750` runtime
directory. Only the worker and grader units receive that supplementary group;
the worker can connect but cannot replace the socket. It is never exposed on
TCP.

## Process contract

Inside the isolated service, each request starts one ephemeral Codex process in
a new empty temporary directory. The verified invocation surface is:

```text
codex --ask-for-approval never exec \
  --ephemeral \
  --sandbox read-only \
  --skip-git-repo-check \
  --json \
  --output-schema evaluation.schema.json \
  --output-last-message evaluation.json -
```

The prompt arrives on standard input, not through a shell-expanded argument.
The process receives only locale, model authentication, Codex home,
certificate, and proxy variables. Its session is ephemeral, approvals are
disabled, and its working directory contains only the output schema and final
response path. See the official [Codex CLI documentation](https://developers.openai.com/codex/cli/)
for current installation and authentication guidance.

## Socket protocol

`POST /v1/grade` accepts only `{"prompt": "..."}`. The request and UTF-8
prompt are independently limited to 2 MiB. Successful responses contain one
`QualitativeEvaluation` plus non-negative token counts. Errors use a bounded,
redacted `model_failure` or `schema_failure` contract.

The stateful worker validates the response again before recording output
digests, usage, cost, status, and duration. Invalid or unavailable grader
responses never update learner evidence.

## Authentication

For unattended operation, put a dedicated model API key only in:

- Compose: `runtime/grader.env`; or
- systemd: `/etc/adaptive-tutor-grader/grader.env` (root-owned, mode `0600`).

Never put a model key in `tutor.env`, `worker.env`, YAML, curriculum prompts,
assignment repositories, or evaluation Actions. Interactive account state may
instead live in the grader's owner-only Codex home, but it must remain isolated
from tutor state.

## Diagnostics

For Compose:

```bash
docker compose --profile remote ps
docker compose logs --tail=100 grader
docker compose exec worker adaptive-tutor doctor --offline
```

For systemd:

```bash
systemctl status adaptive-tutor-grader.service
journalctl -u adaptive-tutor-grader.service --since today
sudo -u adaptive-tutor-grader codex --version
stat -c '%U:%G %a %n' /run/adaptive-tutor-grader \
  /run/adaptive-tutor-grader/grader.sock
```

The doctor reports whether the Unix socket exists and answers its health probe.
Timeouts and transport outages are retryable; malformed model output is a
non-evidence schema failure.
