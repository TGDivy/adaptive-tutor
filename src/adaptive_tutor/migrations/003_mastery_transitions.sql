ALTER TABLE mastery_evidence
ADD COLUMN mastery_before REAL CHECK (mastery_before BETWEEN 0 AND 1);
ALTER TABLE mastery_evidence
ADD COLUMN mastery_after REAL CHECK (mastery_after BETWEEN 0 AND 1);
