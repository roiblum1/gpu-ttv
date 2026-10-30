-- Ledger schema for Release 1 (AGENT.md §20.5, ADR-0001, ADR-0002).
-- Intervals hold physical GPU counts only; money is computed at query time from a pricing snapshot.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    status      text NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed', 'dry_run')),
    coverage    jsonb NOT NULL DEFAULT '{}'::jsonb,
    errors      jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id          text PRIMARY KEY,
    aliases           jsonb NOT NULL DEFAULT '{}'::jsonb,
    vendor            text,
    serial            text,
    system_uuid       text,
    name              text,
    model             text,
    site              text,
    gpu_model         text,
    gpu_count         int,
    source            text,
    first_observed_at timestamptz NOT NULL,
    last_observed_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS capacity_intervals (
    id                        bigserial PRIMARY KEY,
    asset_id                  text NOT NULL REFERENCES assets (asset_id),
    category                  text NOT NULL CHECK (category IN ('PRODUCTIVE', 'IDLE', 'UNKNOWN', 'NOT_CHARGED')),
    reason_code               text NOT NULL,
    stage                     text,
    stage_owner               text,
    stage_entered_at          timestamptz,
    stage_entered_lower_bound boolean NOT NULL DEFAULT false,
    cluster                   text,
    node_uid                  text,
    pool                      text,
    holder_ref                text,
    team                      text,
    gpu_model                 text,
    gpus                      int NOT NULL CHECK (gpus > 0),
    flags                     text[] NOT NULL DEFAULT '{}',
    started_at                timestamptz NOT NULL,
    ended_at                  timestamptz,
    opened_by_scan            bigint REFERENCES scan_runs (id),
    closed_by_scan            bigint REFERENCES scan_runs (id),
    evidence                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    policy_version            text NOT NULL,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

-- One open interval per bucket key; a scan replayed with the same result opens nothing new.
CREATE UNIQUE INDEX IF NOT EXISTS capacity_intervals_open_key
    ON capacity_intervals (asset_id, category, reason_code, coalesce(stage_owner, ''), coalesce(holder_ref, ''),
                           coalesce(pool, ''), coalesce(team, ''), coalesce(gpu_model, ''), gpus)
    WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS capacity_intervals_asset ON capacity_intervals (asset_id, started_at);
CREATE INDEX IF NOT EXISTS capacity_intervals_range ON capacity_intervals (started_at, ended_at);

CREATE TABLE IF NOT EXISTS next_action_annotations (
    id                bigserial PRIMARY KEY,
    asset_id          text NOT NULL REFERENCES assets (asset_id),
    next_action_owner text NOT NULL,
    reason            text NOT NULL,
    created_by        text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    expires_at        timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
    asset_id    text NOT NULL REFERENCES assets (asset_id),
    milestone   text NOT NULL,
    observed_at timestamptz NOT NULL,
    lower_bound boolean NOT NULL,
    evidence    jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (asset_id, milestone)
);

CREATE TABLE IF NOT EXISTS identity_conflicts (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,
    key         text NOT NULL,
    candidates  jsonb NOT NULL,
    first_seen  timestamptz NOT NULL,
    last_seen   timestamptz NOT NULL,
    resolved_at timestamptz,
    resolution  jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS identity_conflicts_open_key ON identity_conflicts (kind, key) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS stage_objectives (
    policy_version text NOT NULL,
    stage          text NOT NULL,
    objective      interval NOT NULL,
    PRIMARY KEY (policy_version, stage)
);

CREATE TABLE IF NOT EXISTS pricing_snapshots (
    snapshot_id     text PRIMARY KEY,
    provider        text NOT NULL,
    region          text NOT NULL,
    purchase_option text NOT NULL,
    captured_at     timestamptz NOT NULL,
    effective_at    timestamptz,
    source_urls     jsonb NOT NULL DEFAULT '[]'::jsonb,
    checksum        text NOT NULL,
    fx_usd_ils      numeric,
    fx_date         date,
    parser_version  text NOT NULL
);

CREATE TABLE IF NOT EXISTS pricing_rates (
    snapshot_id        text NOT NULL REFERENCES pricing_snapshots (snapshot_id),
    gpu_model          text NOT NULL,
    instance_type      text,
    instance_hourly_usd numeric,
    instance_gpus      int,
    per_gpu_hour_usd   numeric,
    mapping            text,
    confidence         text,
    PRIMARY KEY (snapshot_id, gpu_model)
);

-- Engine-private state: source health and the server-scan detail cache.
CREATE TABLE IF NOT EXISTS source_state (
    name             text PRIMARY KEY,
    failed_scans     int NOT NULL DEFAULT 0,
    last_success_at  timestamptz,
    last_attempt_at  timestamptz,
    last_error       text,
    etag             text
);

CREATE TABLE IF NOT EXISTS serverscan_cache (
    id         text PRIMARY KEY,
    row_hash   text NOT NULL,
    fetched_at timestamptz NOT NULL,
    payload    jsonb NOT NULL
);
