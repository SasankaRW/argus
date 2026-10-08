-- Every task learns as it works (P4): your fixes count as verdicts without a click, and answers you confirmed (or
-- left alone) become worked examples for similar inputs ("experience memory"). Examples that keep leading to
-- mistakes are dropped.
ALTER TABLE samples ADD COLUMN subject TEXT;                   -- what the answer was about (file names, one a line)
ALTER TABLE samples ADD COLUMN feedback TEXT;                  -- how the verdict came: you, undo, wrong button,
                                                               -- moved back, renamed, moved, ari
ALTER TABLE samples ADD COLUMN kept_at REAL;                   -- left as it was for a day: an implicit yes
ALTER TABLE samples ADD COLUMN used TEXT;                      -- sample ids given to the model as examples (JSON)
ALTER TABLE samples ADD COLUMN harm INTEGER NOT NULL DEFAULT 0; -- answers marked wrong that had this as an example
CREATE INDEX samples_examples ON samples (playbook, kept_at);
