# Networking fundamentals

A byte stream does not preserve application message boundaries. Protocols need
explicit framing, bounded lengths, partial-read handling, timeouts, and a policy
for propagating backpressure. Datagram designs must state their loss and ordering
assumptions.
