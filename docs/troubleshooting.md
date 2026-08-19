# Troubleshooting

Start with a bounded diagnostic that does not call external services, then add
the integration checks:

```bash
adaptive-tutor doctor --offline
adaptive-tutor doctor
```

The JSON form is useful for supervisors and support bundles:

```bash
adaptive-tutor doctor --offline --json
```

Redact paths or details that identify private repositories or curricula before
sharing output.

## Configuration not found

```text
Configuration not found ... Run 'adaptive-tutor init'.
```

Pass the same global path to every command or export
`ADAPTIVE_TUTOR_CONFIG`. For a service, confirm the unit/container environment
points to a readable mode-0600 file.

## Permission check fails

Private state must not be readable or writable by group/other:

```bash
chmod 0700 /path/to/state
chmod 0600 /path/to/config.yaml /path/to/state/secrets.env
```

Also check ownership under the systemd service account or Compose UID/GID. Do
not “fix” the issue with `0777`.

## Active curriculum is not loaded

Confirm `active_curriculum` matches a package ID, not its display name:

```bash
adaptive-tutor curriculum-load /secure/path/to/package
adaptive-tutor doctor --offline
```

The loader reports a missing file, invalid schema, cyclic prerequisite,
unknown profile weight, or escaped reference path explicitly.

## Codex CLI unavailable or grading fails

Run these as the same Unix/container user as the worker:

```bash
codex --version
adaptive-tutor doctor --offline
```

Verify `PATH`, `HOME`, `CODEX_HOME`, model access, API-key presence in the
worker-only environment, CA/proxy settings, and write access to the Codex home.
Timeouts are retryable; malformed final output is a schema failure and is not
learner evidence. The official [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)
and [non-interactive mode](https://learn.chatgpt.com/docs/codex/non-interactive-mode)
pages document installation and automation authentication.

## GitHub repository check fails

- Confirm the configured owner/repository spelling.
- Verify the App is installed on the selected private workspace.
- Check contents and pull-request write permission plus Actions/check read.
- Confirm the private key belongs to the configured App and installation.
- Ensure the workspace is private; the client intentionally rejects public
  repositories.

Use `https://api.github.com` exactly. The public build rejects alternate API
hosts.

## Webhook is missing or rejected

Run:

```bash
adaptive-tutor webhook-setup
adaptive-tutor doctor
```

A `401` means the signature or local secret does not match. `403` means the
payload names a repository outside the configured workspace. `503` means the
owner or secret is absent. Confirm the public HTTPS proxy forwards the raw body
unchanged and does not strip `X-Hub-Signature-256`, `X-GitHub-Event`, or
`X-GitHub-Delivery`.

## Duplicate webhook delivery

`duplicate: true` is expected after redelivery. The original event and job IDs
are returned and no second evaluation is created. If duplicates appear to
change mastery, stop the worker and preserve the database/logs for a security
and idempotency investigation.

## Worker is retrying or dead-lettering jobs

Inspect worker logs and job counts. Retryable failures back off up to one hour;
expired leases return to the queue after a terminated worker. Non-retryable or
exhausted failures retain a redacted diagnostic as `dead_letter`.

Fix the underlying integration and use the supported reconciliation flow. Do
not edit job rows by hand without an offline backup and a documented recovery
decision.

## Dashboard returns 401

Use the generated API token from the owner-only secrets file. Dashboard login
sets a session cookie for reads; API writes still need
`Authorization: Bearer ...`. Rotating the generated secrets invalidates old
sessions and clients.

## `/readyz` returns 503

The readiness probe requires a healthy SQLite integrity check and at least one
applied migration. Stop the worker, run `doctor --offline`, inspect filesystem
and storage health, and restore a tested backup if integrity fails.

## Restore or upgrade problem

Keep services stopped. Never run an older binary against a newly migrated
database unless release notes guarantee it. Restore the pre-upgrade snapshot
and matching application release, run `doctor --offline`, then restart. See
[Upgrade and rollback](operations.md#upgrade-and-rollback).
