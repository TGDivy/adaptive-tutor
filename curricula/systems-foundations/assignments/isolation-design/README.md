# Design isolation for an untrusted build worker

A persistent tutor service stores a private database, repository-write
credentials, and model authentication. It must evaluate learner-controlled
build commands and collect bounded results. Clearing environment variables and
setting the working directory read-only have been proposed as the sandbox.

In `DESIGN.md`, define attacker capabilities and protected assets, draw the
process and data boundary, specify enforcement for filesystem, identity,
network, processes, memory, CPU, time, and output size, and describe cleanup
after timeout or crash. Explain why each proposed control is necessary and name
the highest residual risk. Report confidence from 0-100.

Target concept: **[[concept_name]]** (`[[concept_id]]`). Format:
**[[exercise_type]]**. Difficulty: **[[difficulty]]/10**. Expected time:
**[[expected_minutes]] minutes**.

Why now: [[selection_reason]] The current learner state is [[state_summary]].
[[difficulty_scope]]
