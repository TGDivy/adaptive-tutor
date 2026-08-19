# Explain a locality benchmark reversal

Two implementations scan the same 24-byte logical records. One stores records
contiguously; the other stores 8-byte pointers to separately allocated records.
At 8 KiB the pointer version is slightly faster. At 64 MiB it is 3.4 times
slower, but the benchmark allocates data inside each timed trial and always runs
the contiguous case first.

In `ANALYSIS.md`, calculate bytes touched for 100,000 and 5,000,000 records,
state two competing hypotheses, design a reproducible benchmark, predict at
least one crossover, and name evidence that would falsify your preferred
explanation. Separate page faults, allocation, cache/TLB behavior, and useful
work. Report confidence from 0-100.

Target concept: **[[concept_name]]** (`[[concept_id]]`). Format:
**[[exercise_type]]**. Difficulty: **[[difficulty]]/10**. Expected time:
**[[expected_minutes]] minutes**.

Why now: [[selection_reason]] The current learner state is [[state_summary]].
[[difficulty_scope]]
