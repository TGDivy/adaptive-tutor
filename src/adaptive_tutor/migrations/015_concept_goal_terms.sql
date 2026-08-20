ALTER TABLE concepts
ADD COLUMN goal_terms_json TEXT NOT NULL DEFAULT '[]';
