---
hide:
  - navigation
  - toc
---

<div class="hero" markdown>
<div markdown>

# Deliberate practice, continuously adapted.

Adaptive Tutor is a self-hosted, Git-native learning engine. It turns durable
evidence from focused pull requests into the next useful challenge—without
letting learner code near your tutor, repository-write, model, or agent
credentials.

[Get started](getting-started.md){ .md-button .md-button--primary }
[See the architecture](architecture.md){ .md-button }

</div>
<div class="hero-card" markdown>

**One local command. No credentials.**

`adaptive-tutor demo`

`✓ curriculum loaded`

`✓ assignment validated`

`✓ evidence evaluated`

`✓ learner model updated`

</div>
</div>

<div class="grid cards" markdown>

-   :material-source-branch: **Git-native assignments**

    ---

    Practice lives in private branches and pull requests. Progressive stages,
    feedback, appeals, and follow-ups stay attached to the work.

-   :material-chart-timeline-variant-shimmer: **Evidence-driven adaptation**

    ---

    Scheduling combines mastery, uncertainty, forgetting, prerequisites,
    confidence, misconception transfer, format diversity, and available time.

-   :material-shield-lock-outline: **Hard trust boundaries**

    ---

    Untrusted submissions run only in credential-free ephemeral CI. Model
    output must pass a strict schema before one transactional state update.

-   :material-database-outline: **Your state, visibly yours**

    ---

    SQLite is the canonical record. Inspect status, readiness, history,
    reports, costs, and evidence through the CLI or authenticated dashboard.

</div>

## The learning loop

```text
curriculum package ──► adaptive scheduler ──► private assignment PR
        ▲                                             │
        │                                  credential-free CI
        │                                             │
        └── spaced retrieval ◄── learner model ◄── structured review
```

The webhook path authenticates, persists, and enqueues quickly. A durable
worker reconciles GitHub evidence, invokes a short-lived read-only Codex worker,
validates the response, and commits the learner-model update. Invalid or
untrusted material never becomes learning evidence by accident.

## Choose a path

- Run the [credential-free local demo](getting-started.md#run-the-local-demo).
- Connect a [least-privilege GitHub App](github.md).
- Author a completely data-driven [curriculum package](curricula.md).
- Operate with [Docker Compose or systemd](operations.md).
- Review the [security boundaries](security.md) before exposing a service.

!!! info "A complete product contract"

    [`SPEC.md`](https://github.com/TGDivy/adaptive-tutor/blob/main/SPEC.md) is
    binding. The repository is complete only when its independent completion
    gate, deployed runtime, and private integration evidence all pass.
