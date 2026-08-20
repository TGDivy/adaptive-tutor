# Adaptation

Adaptive Tutor chooses work from evidence rather than a fixed lesson order.
Each candidate receives an explainable multiplicative priority, then format and
difficulty are selected independently.

## Priority factors

For each concept, the scheduler combines:

| Factor | Effect |
| --- | --- |
| Importance | Curriculum-defined central concepts rise. |
| Weakness | Lower current mastery increases priority. |
| Forgetting | New, due, and overdue retrieval rises over not-yet-due work. |
| Uncertainty | Sparse or ambiguous evidence prompts information-gathering work. |
| Misconception | Severity, frequency, and recurrence increase priority. |
| Profile relevance | Domain or concept weights tailor a curriculum without code changes. |
| Diversity | Recently repeated concepts are discounted. |
| Confidence | Confident failures and overconfident partial work return sooner. |
| Prerequisites | Weak dependencies suppress premature work; weak foundations that unblock dependents rise. |
| Goal focus | Explicit or curriculum-inferred domains/concepts raise relevant work and prerequisite paths. |
| Urgency | An optional target date modestly shifts ordering. |

The command output includes every rounded factor and a concise reason, so a
recommendation is inspectable rather than mysterious.

## Format diversity

Curricula declare supported exercise types per concept. The scheduler intersects
those with the learner's allowed formats and prefers a format that has not just
been used for that concept or overused recently.

Supported forms include implementation, debugging, code review, performance
investigation, written, mathematical, quiz, system design, explanation,
refactoring, and interviewer follow-up. Progressive stages stay in one pull
request rather than manufacturing unrelated work.

## Difficulty

Difficulty is an integer from 1–10. The target starts from curriculum base
difficulty and observed highest success, then adjusts for:

- sustained mastery and repeated success;
- recent success or failure;
- prior failed attempts;
- available time; and
- current energy.

Every adjustment is clamped to the 1–10 contract. A failure lowers the next
challenge rather than blindly escalating; strong evidence at an appropriate
difficulty raises it.

## Spaced retrieval

Successful retrieval extends the review interval. Failures shorten it, and a
confident failure contracts it to a particularly short interval because it is
both a knowledge and calibration signal. Review dates feed the forgetting
factor, closing the loop between evidence and future selection.

## Misconception transfer

A misconception moves through `suspected`, `active`, `challenged`, `resolved`,
and `recurred`. Repeating the same answer cannot resolve it. Resolution requires
successful evidence in a new exercise format and an explicit transfer context.
Later contradictory evidence reopens it as recurred.

## Guardrails

Adaptation is never punishment by infrastructure:

- model, schema, assignment, dependency, and service failures cannot reduce
  mastery;
- hint use is recorded but is not automatic failure;
- invalid model output changes no learner state; and
- appeal reviews append an independent result while retaining the original.
