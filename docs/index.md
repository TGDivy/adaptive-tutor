---
hide:
  - navigation
  - toc
---

<div class="product-intro" markdown>

# Adaptive Tutor

Self-hosted, Git-native adaptive learning. Turn durable evidence from focused
pull requests into the next useful challenge without
letting learner code near your tutor, repository-write, model, or agent
credentials.

[Get started](getting-started.md){ .md-button .md-button--primary }
[View the product](product-tour.md){ .md-button }

</div>

<figure class="product-preview">
  <a href="product-tour/"><img src="assets/screenshots/dashboard.png" alt="Adaptive Tutor dashboard showing an active assignment, progress, reviews, misconceptions, and scores"></a>
  <figcaption>Real credential-free demo state. Generated and freshness-checked from the current product.</figcaption>
</figure>

<div class="grid cards" markdown>

-   **Git-native assignments**

    ---

    Practice lives in private branches and pull requests. Progressive stages,
    feedback, appeals, and follow-ups stay attached to the work.

-   **Evidence-driven adaptation**

    ---

    Scheduling combines mastery, uncertainty, forgetting, prerequisites,
    confidence, misconception transfer, format diversity, and available time.

-   **Hard trust boundaries**

    ---

    Untrusted submissions run only in credential-free ephemeral CI. Model
    output must pass a strict schema before one transactional state update.

-   **Your state, visibly yours**

    ---

    SQLite is the canonical record. Inspect status, readiness, history,
    reports, costs, and evidence through the CLI or authenticated dashboard.

</div>

## The learning loop

<p class="loop-path">
  <span>Curriculum package</span><b>→</b>
  <span>Adaptive scheduler</span><b>→</b>
  <span>Private assignment PR</span><b>→</b>
  <span>Credential-free CI</span><b>→</b>
  <span>Structured review</span><b>→</b>
  <span>Learner model</span><b>→</b>
  <span>Spaced retrieval</span>
</p>

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
