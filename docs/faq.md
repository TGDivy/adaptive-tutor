# FAQ

## Is this a chatbot?

No. Codex performs one bounded qualitative review. Curriculum data, SQLite
evidence, deterministic CI, and application code determine the learning loop.

## Can I try it without credentials?

Yes. `adaptive-tutor demo` runs generation, trusted reference validation,
executes bundled passing and failing submissions against public and hidden
tests, applies qualitative fixture reviews, updates learner state, and reports
progress without network access or credentials. The submissions are
product-owned neutral fixtures; arbitrary learner code remains confined to the
ephemeral evaluator.

## Does it run my code locally?

Only trusted bundled references and neutral demo fixtures run during local
validation. Learner submissions do not run on the persistent tutor host;
remote learner code belongs in credential-free ephemeral CI.

## Why SQLite?

This is a private single-learner service. SQLite provides durable transactions,
foreign keys, migrations, online backup, easy inspection, and low operational
surface. The job queue and learner model need correctness more than a separate
database fleet.

## Can I use another subject?

Yes. Core code is subject-neutral. Supply a separate data package with concepts,
prerequisites, profiles, prompts, references, guidance, and fixtures. Keep
private packages out of this public repository.

## Why require transfer to resolve a misconception?

Repeating an answer can reflect memorization. Success in a new context and
format is stronger evidence that the underlying model changed.

## Are hints penalized?

No automatic penalty. Five progressive levels are tracked as evidence so later
review can distinguish independent retrieval from supported work.

## Can a model failure lower mastery?

No. Invalid output, timeout, dependency, infrastructure, security, assignment,
and generator failures are operational failures. Only valid learner evidence
can update mastery.

## Can I expose the dashboard publicly?

Do not expose it directly. Keep the service loopback-bound and use an
authenticated private tunnel or TLS reverse proxy. An exposed bind requires a
token, but network-level restriction is still expected.

## Why a GitHub App instead of a token?

An App provides selected-repository installation, explicit permissions,
short-lived installation tokens, auditable identity, and webhook integration.
A token is retained only as a development fallback.

## How do appeals work?

The original evaluation remains immutable. An independent schema-valid review
addresses the learner's argument and appends a linked result.

## What should I back up?

The SQLite database, configuration, generated secret file, private curriculum,
GitHub App setup/key recovery material, and Codex authentication needed by your
deployment. Store backups encrypted off-host and test restores.
