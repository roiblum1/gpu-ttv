-- Managers the collector job is configured to read. When a manager leaves TTV_MANAGERS_FILE, the
-- job's next start sets removed_at; the engine, Evidence and alerts then ignore its reads
-- (ADR-0004, "Removing a manager").
CREATE TABLE IF NOT EXISTS collector_managers (
    manager    text PRIMARY KEY,
    kind       text NOT NULL,
    added_at   timestamptz NOT NULL,
    removed_at timestamptz
);
