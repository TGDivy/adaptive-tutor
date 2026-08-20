# Installation

You can evaluate the complete local learning loop without a GitHub account,
model key, private curriculum, or running service. Remote assignments are an
explicit second step. For a live server, use the
[production Compose runbook](operations.md#production-compose-runbook); it is
the supported path that proves every integration before declaring setup ready.

## Requirements

- Python 3.11 or newer
- Git
- `uv` for source development, or another isolated Python package installer
- Docker Engine with Compose v2 for the recommended live deployment
- a public DNS name and inbound TCP 80/443 for automatic TLS
- a GitHub account or organization whose plan supports private-repository
  Actions and default-branch protection
- an OpenAI API key or another authentication method supported by Codex CLI

Compilers and language tooling are curriculum-specific. The bundled demo uses
Python and ships its own fixture review.

## Install from source

```bash
git clone https://github.com/TGDivy/adaptive-tutor.git
cd adaptive-tutor
uv sync --locked --extra dev
uv run adaptive-tutor --version
```

For an isolated command installed from a release checkout:

```bash
uv tool install .
adaptive-tutor --version
```

## Initialize private local state

Choose a configuration and state directory you control:

```bash
adaptive-tutor --config ~/.config/adaptive-tutor/config.yaml init
adaptive-tutor --config ~/.config/adaptive-tutor/config.yaml doctor --offline
```

Initialization:

1. writes mode-0600 YAML containing secret references, never raw tokens;
2. writes generated dashboard and webhook tokens to a separate mode-0600 file;
3. creates an owner-only state directory;
4. applies every SQLite migration; and
5. loads the neutral systems-foundations curriculum.

The same command refuses to overwrite existing configuration or secrets unless
you pass `--force`. Treat force as key rotation: dependent clients and webhook
configuration must be updated afterward.

## Run the local demo

```bash
adaptive-tutor demo
```

The demo really executes:

- curriculum loading and prerequisite validation;
- adaptive concept, format, and difficulty selection;
- assignment generation, consistency checks, and a trusted reference harness;
- execution of product-owned passing and failing submissions against bundled
  fixture checks in a credential-free process;
- schema-valid qualitative fixture review;
- transactional mastery, uncertainty, spacing, and calibration updates; and
- a weekly Markdown and structured-data report.

The scripted submissions are bundled neutral product fixtures, not arbitrary
local code. Live learner submissions use the isolated GitHub-hosted evaluator
described in [Evaluation](evaluation.md).

It deliberately makes no network call and reads no credential. Keep the
resulting SQLite state for inspection when useful:

```bash
adaptive-tutor demo --keep ./demo-state
```

## Explore before connecting GitHub

```bash
adaptive-tutor status
adaptive-tutor readiness
adaptive-tutor concepts
adaptive-tutor next --dry-run --available-minutes 30 --energy low
adaptive-tutor report --period weekly --format markdown
```

`next --dry-run` explains a recommendation without creating a branch or pull
request. A non-dry run requires the private GitHub integration described in
[GitHub App and webhooks](github.md).

## Start the private dashboard

```bash
adaptive-tutor serve
```

Open `http://127.0.0.1:8765/` and sign in with the generated API token. The
dashboard is authenticated by default even on loopback. `/healthz` and
`/readyz` remain unauthenticated so a local supervisor can monitor the process.

Do not bind directly to a public interface. Use the hardened
[deployment paths](operations.md), a loopback-published port, and a trusted
authenticated tunnel or reverse proxy.

## Set the live learning objective

Guided server setup accepts the objective directly:

```bash
adaptive-tutor setup \
  --public-url https://tutor.example.net \
  --goal "Build reliable network services"
```

The active curriculum maps the statement through concept names, domains, and
curriculum-owned `goal_terms`. It persists the inferred concepts as a durable,
revisioned focus. A statement that does not match the active curriculum stops
setup with an actionable error; load a compatible private curriculum or supply
an explicit domain/concept with `goal set` instead of accepting unrelated work.

On a production Compose host, run this through the documented one-shot
`operator` service, not directly on the host. That process has setup state and
read-only grader-socket access without receiving the grader's model credential.

## Next steps

- Understand the [adaptive loop](adaptation.md).
- Configure a [least-privilege GitHub App](github.md).
- Review the [security model](security.md).
- Deploy with [Compose or systemd](operations.md).
