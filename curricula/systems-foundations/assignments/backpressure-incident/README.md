# Diagnose a backpressure incident

A service reads framed requests, places decoded work in an unbounded in-memory
queue, and processes it with four workers. During a load spike, resident memory
grows until the process is terminated. Median service time remains healthy,
while queue residence time, tail latency, retries, and timeouts climb.

Produce `RESPONSE.md` with a causal timeline, three measurements that could
falsify your diagnosis, a bounded design with explicit overload behavior, a
recovery plan, and the strongest new trade-off. Quantify at least one claim.
Keep the response below 900 words and report confidence from 0-100.

Target concept: **[[concept_name]]** (`[[concept_id]]`). Format:
**[[exercise_type]]**. Difficulty: **[[difficulty]]/10**. Expected time:
**[[expected_minutes]] minutes**.

Why now: [[selection_reason]] The current learner state is [[state_summary]].
[[difficulty_scope]]
