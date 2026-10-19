-- What the listener knows about a request (how it was heard: source, the language check, a second listen for "Hey
-- Ari"; when it was spoken), for Helios > Ari > Timeline and `dev.ps1 trace`.
ALTER TABLE ari_turns ADD COLUMN meta TEXT;
