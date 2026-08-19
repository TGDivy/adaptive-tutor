# Repair a missed-shutdown wakeup

```text
take():
  lock(mu)
  if queue.empty(): wait(cv, mu)
  item = queue.pop()
  unlock(mu)
  return item

stop():
  stopping = true
  notify_one(cv)
```

Several consumers may be waiting. Under load tests, shutdown occasionally
hangs; under spurious wakeups, a consumer can pop an empty queue. In
`RESPONSE.md`, show concrete failing schedules, define the protected predicate,
provide corrected pseudocode, and separately argue safety and progress. Include
a deterministic test strategy and confidence from 0-100.

Target concept: **[[concept_name]]** (`[[concept_id]]`). Format:
**[[exercise_type]]**. Difficulty: **[[difficulty]]/10**. Expected time:
**[[expected_minutes]] minutes**.

Why now: [[selection_reason]] The current learner state is [[state_summary]].
[[difficulty_scope]]
