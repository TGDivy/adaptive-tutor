# Defend an engineering decision under pressure

You proposed a bounded in-memory queue with immediate overload responses rather
than an unbounded queue or durable broker. The interviewer says, “That drops
work. Why is your design not just moving the failure to clients?”

In `RESPONSE.md`, write a ninety-second spoken answer. State assumptions,
separate evidence from inference, compare the strongest alternative, and name
the threshold at which you would change designs. Then answer a follow-up: the
requests are now financially irreversible and may be delayed for ten minutes.
Revise the design instead of defending the original choice by reflex. Report
confidence from 0-100.

Target concept: **[[concept_name]]** (`[[concept_id]]`). Format:
**[[exercise_type]]**. Difficulty: **[[difficulty]]/10**. Expected time:
**[[expected_minutes]] minutes**.

Why now: [[selection_reason]] The current learner state is [[state_summary]].
[[difficulty_scope]]
