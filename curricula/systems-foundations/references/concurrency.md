# Concurrency reasoning

Start with the shared-state invariant, define who may mutate it, and specify the
condition for progress and shutdown. A notification is not state; predicates
must be checked while holding the appropriate synchronization boundary.
