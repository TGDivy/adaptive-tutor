# Security

Adaptive Tutor processes hostile repository text and code while holding access
to private learner state, GitHub, and a model. Its primary security property is
separation: untrusted execution never shares a credentialed process boundary.

## Assets

- private curriculum intent and trusted references;
- learner submissions, history, misconceptions, and reports;
- dashboard/personal-agent token and webhook secret;
- GitHub App private key or development token;
- model API key or Codex account state; and
- the integrity of grading and learner-state evidence.

## Trust boundaries

| Input/process | Trust | Allowed capability |
| --- | --- | --- |
| Curriculum package | Operator-trusted, schema-validated | Supply concepts, guidance, references, fixtures. |
| Webhook request | Authenticated but untrusted payload | Persist and enqueue only after HMAC and repository scope checks. |
| Learner repository/content | Untrusted | Execute only in credential-free ephemeral CI. |
| CI artifact | Untrusted until contract/digest validation | Become deterministic evidence, never instructions. |
| Codex output | Untrusted until schema validation | Become qualitative evidence only after validation. |
| Tutor/worker | Trusted | GitHub orchestration and transactional state updates. |

## Untrusted execution

Never run a learner checkout, pull request script, build system, or arbitrary
test command on the persistent tutor host. The private workspace workflow must
use an ephemeral hosted runner and a credential-free isolated process/container
with no network where practical.

Checkout actions must not persist credentials. Job permissions must be
read-only. Do not reference secrets in a job that later invokes learner code,
and never use an untrusted workflow revision with `pull_request_target`.

## Prompt injection

Learner text, repository files, compiler logs, CI output, and appeal arguments
are delimited untrusted data. They cannot change grading instructions, request
tools, reveal secrets, or override the output schema. Injection-like patterns
are surfaced as flags, not followed.

The Codex subprocess receives no GitHub-write, dashboard, webhook, or agent
credential. Its sandbox is read-only, approvals are disabled, the session is
ephemeral, and the working directory contains only the schema/output contract.

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

The public-boundary gate scans every tracked file for private subject material,
organization infrastructure, key blocks, and token formats. It complements—
but does not replace—repository secret scanning and human review.

## GitHub scope

Use a dedicated App installed only on the private learning and curriculum
repositories. Verify the workspace is private before publishing. Branch paths,
artifact zip entries, sizes, and evidence contracts are validated. Public pull
requests must never be routed to a credentialed evaluator.

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
