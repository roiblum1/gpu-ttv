-- AWS On-Demand comparison pricing in USD (ADR-0003). Intervals still hold GPU counts only;
-- value = GPU-hours x per_gpu_hour_usd, joined interval gpu_model -> alias -> canonical -> rate.

-- Raw GPU model strings as the engine sees them -> canonical catalogue name (NULL = unidentified).
CREATE TABLE IF NOT EXISTS gpu_model_aliases (
    raw_model       text PRIMARY KEY,
    canonical       text,
    catalog_version text NOT NULL,
    updated_at      timestamptz NOT NULL
);

-- Snapshot provenance. effective_at = publication_date: a snapshot is in force from publication.
ALTER TABLE pricing_snapshots
    ADD COLUMN IF NOT EXISTS service          text,
    ADD COLUMN IF NOT EXISTS currency         text,
    ADD COLUMN IF NOT EXISTS operating_system text,
    ADD COLUMN IF NOT EXISTS tenancy          text,
    ADD COLUMN IF NOT EXISTS billing_unit     text,
    ADD COLUMN IF NOT EXISTS publication_date timestamptz,
    ADD COLUMN IF NOT EXISTS offer_version    text,
    ADD COLUMN IF NOT EXISTS raw_sha256       text,
    ADD COLUMN IF NOT EXISTS content_sha256   text,
    ADD COLUMN IF NOT EXISTS map_version      text,
    ADD COLUMN IF NOT EXISTS catalog_version  text,
    ADD COLUMN IF NOT EXISTS imported_at      timestamptz,
    ADD COLUMN IF NOT EXISTS document         jsonb;
CREATE INDEX IF NOT EXISTS pricing_snapshots_publication
    ON pricing_snapshots (region, publication_date, captured_at);

-- Rates stay keyed by (snapshot, canonical model). Unpriced models are rows with mapping
-- 'unpriced' and NULL prices, so their reason is visible next to the priced ones.
ALTER TABLE pricing_rates
    ADD COLUMN IF NOT EXISTS sku       text,
    ADD COLUMN IF NOT EXISTS rate_code text,
    ADD COLUMN IF NOT EXISTS reason    text;

-- The present-day snapshot the engine selected (TTV_PRICING_SNAPSHOT, else the latest);
-- Grafana's snapshot variable defaults to it. One row.
CREATE TABLE IF NOT EXISTS pricing_selection (
    singleton   boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    snapshot_id text NOT NULL REFERENCES pricing_snapshots (snapshot_id),
    selected_at timestamptz NOT NULL,
    source      text NOT NULL
);
