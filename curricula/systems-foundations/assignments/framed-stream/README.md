# Harden a framed stream decoder

The protocol uses a two-byte unsigned big-endian payload length followed by
that many payload bytes. `FrameDecoder.feed` receives arbitrary stream chunks.
The current implementation assumes one complete frame per read.

Review and repair the implementation. It must buffer split headers and
payloads, emit all complete frames in order, retain only an incomplete suffix,
and reject declared lengths above `max_frame_size`. Add at least two boundary
tests. In `REVIEW.md`, state the buffer invariant, describe the security impact
of an unchecked length, and report confidence from 0-100.

Target concept: **[[concept_name]]** (`[[concept_id]]`). Format:
**[[exercise_type]]**. Difficulty: **[[difficulty]]/10**. Expected time:
**[[expected_minutes]] minutes**.

Why now: [[selection_reason]] The current learner state is [[state_summary]].
[[difficulty_scope]]
