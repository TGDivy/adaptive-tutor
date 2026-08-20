# CLI reference

Every stateful command accepts the global `--config PATH` option (or the
`ADAPTIVE_TUTOR_CONFIG` environment variable). Add `--json` where supported for
stable machine-readable output.

```bash
adaptive-tutor --help
adaptive-tutor COMMAND --help
```

## Learning workflow

| Command | Purpose |
| --- | --- |
| `init` | Create secure configuration, migrate SQLite, and load the bundled curriculum. |
| `doctor` | Check configuration, permissions, database, tooling, Codex, GitHub, webhook, and service health. |
| `status` | Summarize runtime state, active work, reviews, misconceptions, readiness, and model cost. |
| `next` | Select and publish the next assignment; `--dry-run` only recommends. |
| `current` | Show the active assignment without private evaluator or reference material. |
| `hint` | Reveal and record the next of five progressive hint levels. |
| `readiness` | Show weighted readiness and uncertainty by domain. |
| `concepts` | Inspect mastery, uncertainty, evidence, spacing, calibration, and trend by concept. |
| `history` | List assignment status, attempts, and structured review scores. |
| `review [ASSIGNMENT_ID]` | Show the latest or selected complete review, dimensions, feedback, follow-up, attempts, and pull request. |
| `goal show` / `goal set` / `goal history` | Manage the durable, revisioned learning goal and optional curriculum focus. |
| `report` | Generate a weekly or monthly console, Markdown, or JSON report. |
| `pause` / `resume` | Stop or resume new assignment creation without discarding evaluation jobs. |
| `demo` | Execute the credential-free product flow with bundled neutral submissions. |

### Context-aware recommendation

```bash
adaptive-tutor next \
  --dry-run \
  --available-minutes 25 \
  --energy low \
  --days-until-goal 14 \
  --json
```

The response exposes the chosen concept, format, target difficulty, priority,
factor values, and explanation. It never exposes trusted answer material.

### Learning goals

```bash
adaptive-tutor goal set "Build reliable network services" \
  --domain networking \
  --concept networking.flow-control \
  --target-date 2026-12-31
adaptive-tutor goal show --json
adaptive-tutor goal history
```

Goal revisions are retained. Explicit concept/domain focus contributes a
bounded scheduling factor, including prerequisite paths; a saved target date
supplies the default urgency horizon. Free-form text is retained as the
operator's objective but is not interpreted as a curriculum package.

### Completed reviews

```bash
adaptive-tutor review
adaptive-tutor review A-0004 --json
```

The projection includes dimension rationale, feedback, follow-up, every attempt
and score, and the pull-request URL when the assignment was published remotely.

### Reports

```bash
adaptive-tutor report --period weekly
adaptive-tutor report --period monthly --format markdown --output progress.md
adaptive-tutor report --period weekly --format json --output progress.json
```

Report periods use current UTC time unless an internal controlled test supplies
an explicit end. Re-running the exact same period is idempotent.

### Offline diagnostics

```bash
adaptive-tutor doctor --offline
adaptive-tutor doctor --offline --strict
adaptive-tutor doctor --offline --json
```

Offline mode skips GitHub calls. Strict mode turns warnings—such as a stopped
service or intentionally disabled Codex worker—into a nonzero exit status.

## Operations

| Command | Purpose |
| --- | --- |
| `serve` | Run the dashboard, personal-agent API, health probes, and signed webhook receiver. |
| `worker` | Lease and process durable event jobs; `--once` is useful for controlled checks. |
| `backup [PATH]` | Create an online, mode-0600 SQLite backup and verify its destination. |
| `restore PATH --yes` | Replace state from an integrity-checked backup while services are stopped. |
| `curriculum-load PATH` | Validate and persist a curriculum package without core-code changes. |
| `webhook-setup` | Create or reconcile the configured signed repository webhook. |
| `evaluate-public` | Internal protected-workflow entry point that verifies a signed public manifest and writes normalized evidence. |

`serve`, `worker`, `grader`, and `evaluate-public` are hidden from the concise
interactive help because they are service or protected-workflow entry points,
not everyday learning commands. Their behavior is covered in [Deployment and
recovery](operations.md) and [Evaluation](evaluation.md).

## Exit behavior

- Success returns zero.
- Configuration, validation, security, or integration errors return nonzero and
  print a bounded diagnostic.
- JSON commands put machine-readable data on standard output.
- Raw tokens, private keys, trusted solutions, and private evaluator material
  are never printed by normal status commands.
