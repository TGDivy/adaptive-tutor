# Repair a rolling event counter

`src/rolling_counter.py` counts events in a half-open moving window. Its
expiration loop mutates a list while iterating and can retain expired events.
Repair it under these constraints:

1. timestamps passed to `record` are nondecreasing;
2. `count(now)` retains exactly timestamps where `now - timestamp < window`;
3. repeated calls without new events are idempotent;
4. construction rejects a non-positive window;
5. the public API remains unchanged.

Add a regression test with several consecutive expired events. In `ANSWER.md`,
state the retained-range invariant, explain the failure mechanism, compare one
alternative representation, and report confidence from 0-100.

Target concept: **[[concept_name]]** (`[[concept_id]]`). Format:
**[[exercise_type]]**. Difficulty: **[[difficulty]]/10**. Expected time:
**[[expected_minutes]] minutes**.

Why now: [[selection_reason]] The current learner state is [[state_summary]].
[[difficulty_scope]]
