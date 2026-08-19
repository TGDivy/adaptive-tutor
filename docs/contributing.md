# Contributing

Contributions are welcome when they preserve the complete product contract,
privacy boundary, and evidence-driven behavior.

## Set up

```bash
git clone https://github.com/TGDivy/adaptive-tutor.git
cd adaptive-tutor
uv sync --locked --extra dev
```

Use small, coherent commits. Keep unrelated working-tree changes intact and
never commit local state, private curricula, learner data, environment files,
keys, tokens, generated evidence, or workstation-specific paths.

## Required checks

```bash
UV_NO_CONFIG=1 uv run --locked ruff check src tests scripts
UV_NO_CONFIG=1 uv run --locked mypy src/adaptive_tutor
UV_NO_CONFIG=1 uv run --locked pytest -q -W error
./scripts/check-public-boundary
./scripts/check-deployment
UV_NO_CONFIG=1 uv run --locked mkdocs build --strict
```

Run the narrowest relevant test while developing, then the complete set before
requesting review. New behavior needs failure-path, idempotency, and security
tests—not only a happy-path assertion.

## Dependency changes

Resolve only against public PyPI and scan the resulting lock before commit:

```bash
UV_NO_CONFIG=1 uv lock \
  --default-index https://pypi.org/simple \
  --system-certs
```

Keep runtime dependencies minimal. Container base images, GitHub Actions, and
tool versions are pinned. Update them deliberately with build/test evidence and
security review.

## Design expectations

- Keep curriculum knowledge in packages, not conditionals in core code.
- Preserve deterministic evidence separately from qualitative judgment.
- Treat repository text, learner content, CI logs, and model output as
  untrusted.
- Reject invalid contracts before a learner-state transaction.
- Make webhook/event operations idempotent and restart-safe.
- Classify operational failures; never turn them into negative learner evidence.
- Keep CLI output concise and JSON projections stable.
- Add a numbered migration for schema changes and test upgrade/idempotency.

## Documentation

Update the relevant guide and tested command snippets with product behavior.
Screenshots must be reproducible from neutral local or controlled private
fixtures, contain no credential or learner data, and include their generation
instructions.

## Pull requests

Explain the user-visible outcome, trust-boundary impact, migration behavior,
and verification performed. Keep a PR focused enough to review. Security fixes
with exploit details should use GitHub private vulnerability reporting rather
than a public issue.

The binding completion contract is [SPEC.md](specification.md). Do not weaken a
gate or ledger entry merely to make a partial implementation appear complete.
