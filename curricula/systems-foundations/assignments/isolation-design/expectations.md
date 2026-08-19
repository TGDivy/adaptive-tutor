Environment filtering is defense in depth, not isolation. A strong design uses
a separate principal and ephemeral execution boundary with no private mounts,
explicitly controlled egress, bounded resources, and a supervisor outside the
boundary. It validates output as untrusted data and proves descendant cleanup.
The answer distinguishes confidentiality, integrity, availability, and residual
kernel or runtime risk.
