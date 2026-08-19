# Explain a tail-latency collapse

A service has eight workers. At 2,400 requests/second, mean CPU service time is
2.6 ms and p99 end-to-end latency is 18 ms. Increasing load to 2,800
requests/second raises mean CPU service time to 2.8 ms but p99 latency to 410
ms. CPU utilization is reported as 92%; voluntary context switches and one
shared-lock wait both rise.

In `ANALYSIS.md`, calculate offered worker demand at both loads, explain what
the averages can and cannot prove, give three competing causal models, and
design measurements that distinguish them. Recommend mitigations only after
stating which observation would justify each one. Report confidence from
0-100.

Target concept: **[[concept_name]]** (`[[concept_id]]`). Format:
**[[exercise_type]]**. Difficulty: **[[difficulty]]/10**. Expected time:
**[[expected_minutes]] minutes**.

Why now: [[selection_reason]] The current learner state is [[state_summary]].
[[difficulty_scope]]
