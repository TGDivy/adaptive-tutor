# Repair a bounded work queue

The queue in `src/bounded_queue.py` confuses capacity with current occupancy
after removals and insertions. Repair it while preserving these observable
constraints:

1. `put` returns `False` only when exactly `capacity` items are present;
2. `get` returns `None` only when no items are present;
3. values leave in insertion order through storage wraparound;
4. construction rejects non-positive capacity;
5. no operation changes configured capacity.

Add at least one focused regression test. Run `python -m pytest -q`. In
`ANSWER.md`, state the invariant, explain why the original representation loses
it, compare one credible alternative, and report confidence from 0-100.

Target concept: **[[concept_name]]** (`[[concept_id]]`). Format:
**[[exercise_type]]**. Difficulty: **[[difficulty]]/10**. Expected time:
**[[expected_minutes]] minutes**.

Why now: [[selection_reason]] The current learner state is [[state_summary]].
[[difficulty_scope]]
