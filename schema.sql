-- SQLite stores UUIDs as TEXT and timestamps here as UTC Unix seconds.
CREATE TABLE IF NOT EXISTS secrets (
    id TEXT PRIMARY KEY NOT NULL CHECK (length(id) = 36),
    payload TEXT NOT NULL
        CHECK (length(CAST(payload AS BLOB)) BETWEEN 1 AND 65536),
    created_at TIMESTAMP NOT NULL DEFAULT (unixepoch())
        CHECK (typeof(created_at) = 'integer')
);

-- Avoid a full table scan when cleaning up expired ciphertext.
CREATE INDEX IF NOT EXISTS secrets_created_at ON secrets (created_at);
