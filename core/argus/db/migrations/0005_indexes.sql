-- Indexes for the reads Helios makes all day: events by component (the inspector) and the newest jobs.
CREATE INDEX IF NOT EXISTS events_from ON events (from_component, at);
CREATE INDEX IF NOT EXISTS events_to ON events (to_component, at);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created_at);
