The pipe ends, buffer, and child process are separate resources. A strong review
does not collapse partial child creation into a boolean result. It establishes
who closes each descriptor, who terminates and reaps a child, how cancellation
interacts with startup, and how the original error is preserved. Tests repeat
each injected failure and observe both descriptor count and process lifecycle.
