# Resource lifetime

Make ownership explicit. Every acquired resource needs one deterministic release
path that remains correct across early return, cancellation, and errors. Prefer
small ownership-bearing types over comments that ask callers to remember cleanup.
