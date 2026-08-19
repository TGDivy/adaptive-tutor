CREATE UNIQUE INDEX qualitative_initial_attempt_idx
ON qualitative_evaluations(attempt_id)
WHERE review_kind = 'initial';
