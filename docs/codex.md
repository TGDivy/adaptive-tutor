# Codex worker

Adaptive Tutor uses Codex as a short-lived qualitative grader, not as its state
store or scheduler. The worker receives one bounded prompt and must produce one
schema-valid final response.

## Supported interface

The implementation follows the official OpenAI documentation for
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli) and
[non-interactive `codex exec`](https://learn.chatgpt.com/docs/codex/non-interactive-mode).
Those docs describe repeatable pipeline use, explicit sandbox settings,
`--output-schema`, ephemeral runs, and API-key authentication for automation.

The effective invocation uses:

```text
codex --ask-for-approval never exec \
  --ephemeral \
  --sandbox read-only \
  --skip-git-repo-check \
  --json \
  --output-schema evaluation.schema.json \
  --output-last-message evaluation.json -
```

The prompt arrives on standard input rather than a shell-expanded argument. A
configured model is passed as an explicit argument; otherwise the authenticated
Codex installation chooses its configured default.

## Authentication

For unattended workers, use a dedicated, scoped API key available only to the
worker process. Docker Compose reads it only from `runtime/worker.env`; the
systemd worker reads only `/etc/adaptive-tutor/worker.env`. Interactive Codex
account authentication can live in an owner-only `CODEX_HOME`, but API keys are
the simpler rotation path for automation.

Never place a model key in curriculum prompts, assignment repositories,
dashboard configuration, CI evidence, or GitHub Actions that execute learner
code.

## Environment filtering

The child receives a small allowlist: locale, timezone, home, Codex home, model
authentication, certificate, and proxy variables. Repository-write,
dashboard, webhook, and personal-agent variables are excluded. Git interactive
credential prompts and system Git configuration are disabled.

The worker runs in a new temporary directory, with an ephemeral Codex session
and read-only sandbox. It has no learner repository checkout to modify.

## Output and usage

JSON-lines process events are used only to collect token usage. The final
structured output file is validated against `QualitativeEvaluation` before
being persisted. Model invocation rows record purpose, model, prompt version,
input/output digests, token counts, configured cost, status, duration, and a
redacted bounded error when applicable.

Prompts and model outputs are not trusted merely because they came from a model.
The application schema and transaction boundary remain authoritative.

## Diagnostics

```bash
codex --version
adaptive-tutor doctor --offline
adaptive-tutor worker --once
```

If grading times out, verify CLI authentication under the service account,
certificate/proxy settings, `CODEX_HOME` permissions, model access, and the
configured timeout. See [Troubleshooting](troubleshooting.md).
