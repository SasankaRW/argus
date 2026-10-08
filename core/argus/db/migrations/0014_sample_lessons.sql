-- Which approved lessons were in force when a model answered (Helios shows "lessons used" on the answer, and the
-- learning chart compares answers before and after each lesson).
ALTER TABLE samples ADD COLUMN lessons_id INTEGER REFERENCES lessons(id);
