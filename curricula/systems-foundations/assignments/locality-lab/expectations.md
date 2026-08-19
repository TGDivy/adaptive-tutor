Pointer layout adds an 8-byte pointer per logical record and usually touches a
second allocation with allocator metadata and weaker spatial locality. A sound
experiment removes allocation from timed work, separates cold and warm trials,
randomizes order, checks generated code, reports distributions, and records page
faults plus cache/TLB counters when available. Conclusions identify what the
measurements do not establish.
