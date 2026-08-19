# Adaptive Tutor — public product specification

Adaptive Tutor is a generic, self-hosted, Git-native adaptive learning engine.
This document is the binding completion contract for the public product. A
completed build must satisfy every requirement here and pass
`./scripts/check-completion`; passing a narrow test subset is not completion.

## Product experience

- Package a polished `adaptive-tutor` CLI supporting `init`, `doctor`,
  `status`, `next`, `current`, `hint`, `readiness`, `report`, `history`,
  `concepts`, `pause`, `resume`, and `demo`.
- The local demo must need no credentials and must exercise scheduling,
  assignment generation and validation, deterministic and qualitative fixture
  evaluation, transactional learner-state updates, and reporting.
- Ship a responsive, authenticated-by-default private dashboard showing the
  active curriculum and assignment, readiness, weaknesses, misconceptions,
  reviews, scores, activity, model usage and cost, and weekly/monthly progress.
- Provide a machine-readable API for status, readiness, active assignment,
  assignment creation, report generation, pause, and resume, accepting context
  such as available time and energy.

## Curriculum and adaptation

- Load curricula entirely from data packages containing definitions, concepts,
  prerequisites, profiles, references, prompts, fixtures, generation guidance,
  and grading guidance. Core code must not assume a particular subject.
- Bundle a polished neutral systems-foundations curriculum and document private
  external curriculum packages.
- Persist per-concept mastery, uncertainty, evidence counts, successes,
  failures, highest difficulty, recent/long-term performance, review dates,
  confidence calibration, and trend, while retaining all historical evidence.
- Model misconceptions through suspected, active, challenged, resolved, and
  recurred states. Resolution requires successful transfer evidence in a new
  context, never a repeated answer alone.
- Schedule using importance, weakness, forgetting, uncertainty,
  misconceptions, profile relevance, confidence, format/topic diversity, and
  prerequisite weakness. Adapt difficulty on a 1–10 scale and implement spaced
  retrieval with shorter intervals after confident failures.
- Support implementation, debugging, code review, performance investigation,
  written, mathematical, quiz, system-design, explanation, refactoring, and
  interviewer-style follow-up assignments. Progressive stages stay in one PR.

## Generation and evaluation

- Assignment generation consumes a scoped learner-state projection, concepts,
  misconceptions, recent work, difficulty, available time, formats, profile,
  and trusted references. Output includes metadata, instructions, starter
  files, tests, hidden evaluator metadata, rubric, tags, duration, and a trusted
  reference expectation.
- Validate consistency, information sufficiency, solvability, reference
  behavior, test alignment, difficulty, concept coverage, ambiguity, and
  repetition before publication. Execute trusted reference solutions against
  their harness where applicable.
- Run learner code only in credential-free ephemeral CI. Normalize compiler,
  test, sanitizer, static-analysis, benchmark, output, allocation, and policy
  evidence where relevant.
- Invoke Codex as a short-lived worker and require JSON-Schema-valid qualitative
  output: overall/dimension scores, grader confidence, concept evidence,
  misconceptions, feedback, follow-up, and escalation. Invalid model output
  must never modify learner state.
- Keep trusted instructions, rubrics, references, CI evidence, and untrusted
  learner content explicitly separated. Detect and neutralize prompt-injection
  attempts. Distinguish wrong, incomplete, weakly justified, valid alternative,
  style preference, and trade-off findings.
- Preserve original grading on appeal, perform an independent stronger review,
  and append the outcome. Track five progressive hint levels without treating
  hint requests as automatic failure.

## GitHub and event workflow

- Support least-privilege GitHub App authentication, branch/PR creation,
  Actions/check and artifact reads, reviews/comments, and webhook delivery.
- Verify webhook signatures; persist and idempotently enqueue push,
  pull-request, workflow/check, and issue-comment events; respond before doing
  long work. Poll only for reconciliation.
- Persist jobs across restarts with retries and dead-letter diagnostics. Classify
  learner, infrastructure, generator, assignment, model, schema, security, and
  dependency failures; only learner evidence may reduce mastery.
- Demonstrate the controlled end-to-end path from curriculum loading through a
  real private branch/PR, CI artifact, structured review, state update, next
  challenge, CLI/dashboard/report visibility, and duplicate-delivery safety.

## Storage, security, and operations

- Use transactional SQLite with versioned migrations for curricula, concepts,
  relationships, assignments/stages/concepts, attempts, automated and
  qualitative evaluations, appeals, mastery and evidence, misconceptions and
  evidence, confidence, events, jobs, hints, reports, model invocations, prompt
  versions, activity, and configuration.
- Keep untrusted execution physically separate from tutor, GitHub-write,
  model, and personal-agent credentials. Never execute arbitrary public-PR code
  on the persistent tutor host.
- Bind the dashboard to loopback by default, require authorization when exposed,
  verify configuration and filesystem permissions, redact secrets, and provide
  health/readiness checks.
- Supply Docker Compose and systemd deployment paths plus install, configure,
  start, stop, restart, status, logs, backup, restore, upgrade, and disaster
  recovery instructions. Services recover after reboot, crash, network loss,
  and terminated model workers.

## Public quality

- Maintain excellent concise-first README documentation, architecture,
  onboarding, security, curriculum, deployment, FAQ, and contribution guidance.
- Build and automatically deploy a polished documentation site covering setup,
  CLI, operations, GitHub App/webhooks, authoring, adaptation, learner model,
  evaluation, Codex, security, architecture, API, troubleshooting, and
  contribution.
- Include reproducible, real, neutral screenshots for setup, doctor,
  status/readiness, assignment creation, example PR, CI result, review,
  follow-up, report, and dashboard, plus a short terminal animation where
  practical.
- Test units, integration, end-to-end flow, migrations, scheduler, adaptation,
  review spacing, misconceptions, calibration, curricula, assignment
  validation, schemas, GitHub events, idempotency, CLI, dashboard, security,
  isolation, injection, recovery, deployment, documentation snippets, and the
  completion checker.
- The public repository and all generated/frozen artifacts must contain only
  neutral product material and public infrastructure references. Private
  curricula and learner work live in separately access-controlled repositories.

## Completion evidence

`implementation/completion.json` maps every section above to independent,
current evidence. `scripts/check-completion` must run tests, migrations, CLI and
demo smoke tests, docs build, screenshot checks, curriculum checks, integration
and event-flow tests, learner/scheduler/report/dashboard/security checks,
privacy scans, deployment validation, and controlled end-to-end verification.
The project is complete only when that gate passes and the deployed system and
required private integrations are proven operational.
