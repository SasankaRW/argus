-- Plugin trace events (file.*, plugin.*, http.*) no longer name a destination; drop the stray lines they drew
-- to boxes called "file", "plugin" and "http".
DELETE FROM edges WHERE dst IN ('file', 'plugin', 'http');
