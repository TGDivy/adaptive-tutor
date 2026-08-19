The durable predicate is a function of queue state and shutdown state, protected
by the same mutex used by the condition variable. Waiting is a loop, because a
wakeup grants no right to proceed. Shutdown mutates protected state before a
broadcast. Strong answers show at least one lost-wakeup schedule, one spurious
wakeup schedule, and a deterministic way to force the relevant handoffs.
