-- Profile-less GPU collector (ADR-0004, ADR-0002 rows 3-5). One row per manager read and one per
-- server seen in it. The engine reads each manager's latest complete read; history stays.

CREATE TABLE IF NOT EXISTS collector_reads (
    id          bigserial PRIMARY KEY,
    manager     text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('ucs_central', 'openmanage')),
    started_at  timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    complete    boolean NOT NULL,
    coverage    jsonb NOT NULL DEFAULT '{}'::jsonb,
    errors      jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS collector_reads_latest ON collector_reads (manager, complete, finished_at DESC);

CREATE TABLE IF NOT EXISTS collector_servers (
    read_id           bigint NOT NULL REFERENCES collector_reads (id) ON DELETE CASCADE,
    vendor            text NOT NULL,
    serial_normalized text NOT NULL,
    source_id         text NOT NULL,
    name              text,
    model             text,
    profile_name      text,
    gpu_models        jsonb,           -- NULL = GPU inventory unread; [] = no GPUs
    domain            text,
    evidence          jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (read_id, source_id)
);
CREATE INDEX IF NOT EXISTS collector_servers_identity ON collector_servers (vendor, serial_normalized);

-- First time the collector saw each server (the DC stage clock is a lower bound from here).
CREATE TABLE IF NOT EXISTS collector_first_seen (
    vendor            text NOT NULL,
    serial_normalized text NOT NULL,
    first_seen_at     timestamptz NOT NULL,
    PRIMARY KEY (vendor, serial_normalized)
);

-- First time each profile name was seen bound to a server: the handover clock of row 5.
CREATE TABLE IF NOT EXISTS collector_profile_first_seen (
    vendor            text NOT NULL,
    serial_normalized text NOT NULL,
    profile_name      text NOT NULL,
    first_seen_at     timestamptz NOT NULL,
    PRIMARY KEY (vendor, serial_normalized, profile_name)
);
