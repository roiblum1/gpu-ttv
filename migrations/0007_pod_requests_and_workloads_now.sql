-- ADR-0009: request counters read from each vLLM pod, and the live per-workload view of the team pages.

-- One reading of a serving pod's request counter per scan; the engine keeps demand_window + 1 h of them.
-- Written in dry run too: like source_state it is operating state, not the ledger.
CREATE TABLE IF NOT EXISTS serving_counter_samples (
    cluster        text NOT NULL,
    namespace      text NOT NULL,
    pod            text NOT NULL,
    holder_ref     text NOT NULL,
    observed_at    timestamptz NOT NULL,
    value          double precision NOT NULL CHECK (value >= 0),
    pod_started_at timestamptz,
    restarts       int NOT NULL DEFAULT 0 CHECK (restarts >= 0),
    PRIMARY KEY (cluster, namespace, pod, observed_at)
);
CREATE INDEX IF NOT EXISTS serving_counter_samples_time ON serving_counter_samples (observed_at);

-- Every GPU-holding logical workload in the target clusters as of the last writing scan, one row per
-- (cluster, workload, reason). A writing scan replaces the whole table, so it never holds stale rows.
CREATE TABLE IF NOT EXISTS workloads_now (
    cluster               text NOT NULL,
    holder_ref            text NOT NULL,
    reason_code           text NOT NULL,
    category              text NOT NULL CHECK (category IN ('PRODUCTIVE', 'IDLE', 'UNKNOWN', 'NOT_CHARGED')),
    kind                  text NOT NULL CHECK (kind IN ('serving', 'ml', 'unresolved')),
    runai                 boolean NOT NULL DEFAULT false,
    stage                 text,
    stage_owner           text,
    gpus                  int NOT NULL CHECK (gpus > 0),
    gpu_models            text[] NOT NULL DEFAULT '{}',
    requests_window       double precision,
    window_hours          double precision,  -- hours requests_window covers (24, or less while filling)
    window_complete       boolean,
    requests_recent       double precision,
    recent_hours          double precision,
    recent_complete       boolean,
    requests_per_gpu_hour double precision,
    gpu_busy_peak         double precision CHECK (gpu_busy_peak IS NULL OR gpu_busy_peak >= 0),
    busy_window_hours     double precision,
    gpu_power_peak        double precision CHECK (gpu_power_peak IS NULL OR gpu_power_peak >= 0),
    gpu_power_avg         double precision CHECK (gpu_power_avg IS NULL OR gpu_power_avg >= 0),
    since                 timestamptz,
    updated_at            timestamptz NOT NULL,
    scan_id               bigint REFERENCES scan_runs (id),
    PRIMARY KEY (cluster, holder_ref, reason_code)
);
