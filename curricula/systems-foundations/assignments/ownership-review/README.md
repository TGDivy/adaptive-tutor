# Review a leaking worker launch path

```python
def launch(command):
    read_fd, write_fd = os.pipe()
    buffer = bytearray(64 * 1024)
    child = spawn(command, stdout=write_fd)
    if not child.ready(timeout=0.2):
        return None
    os.close(write_fd)
    return Worker(child, read_fd, buffer)
```

Production reports show descriptor growth after failed launches and occasional
children that outlive a cancelled request. Review the code without assuming
`spawn` or `ready` is all-or-nothing. In `REVIEW.md`, rank findings by impact,
write an ownership table at each transition, propose the smallest robust
refactor, and design two failure-injection tests. Distinguish correctness from
style and report confidence from 0-100.

Target concept: **[[concept_name]]** (`[[concept_id]]`). Format:
**[[exercise_type]]**. Difficulty: **[[difficulty]]/10**. Expected time:
**[[expected_minutes]] minutes**.

Why now: [[selection_reason]] The current learner state is [[state_summary]].
[[difficulty_scope]]
