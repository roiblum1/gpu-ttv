"""Canonical ledger queries; Grafana panels use the same logic with its time macros (§20.11)."""

# Idle GPU-hours per stage owner inside [from, to); open intervals run to `now`; all are clipped.
IDLE_GPU_HOURS_BY_OWNER = """
SELECT i.stage_owner,
       sum(i.gpus * extract(epoch FROM
             least(coalesce(i.ended_at, %(now)s), %(to)s)
           - greatest(i.started_at, %(from)s)) / 3600) AS idle_gpu_hours
FROM capacity_intervals i
WHERE i.category = 'IDLE'
  AND least(coalesce(i.ended_at, %(now)s), %(to)s) > greatest(i.started_at, %(from)s)
GROUP BY i.stage_owner
"""

# Overdue GPU-hours: the part of each IDLE interval after stage_entered_at + its stage objective.
OVERDUE_GPU_HOURS_BY_OWNER = """
SELECT i.stage_owner,
       sum(i.gpus * extract(epoch FROM
             least(coalesce(i.ended_at, %(now)s), %(to)s)
           - greatest(i.started_at, i.stage_entered_at + o.objective, %(from)s)) / 3600)
         AS overdue_gpu_hours
FROM capacity_intervals i
JOIN stage_objectives o ON o.stage = i.stage AND o.policy_version = i.policy_version
WHERE i.category = 'IDLE'
  AND least(coalesce(i.ended_at, %(now)s), %(to)s)
      > greatest(i.started_at, i.stage_entered_at + o.objective, %(from)s)
GROUP BY i.stage_owner
"""

# Current GPUs by category: the mutually exclusive partition of every observed GPU right now.
CURRENT_GPUS_BY_CATEGORY = """
SELECT category, sum(gpus) AS gpus FROM capacity_intervals WHERE ended_at IS NULL GROUP BY category
"""

# AWS-equivalent value in USD (ADR-0003, AGENT.md §10): a comparison, not a bill or a loss.
# Value = GPU-hours x per_gpu_hour_usd via interval gpu_model -> gpu_model_aliases -> pricing_rates.
# NULL value = nothing priced; unpriced GPU-hours are always returned beside it, never as $0.

# Idle and overdue GPU-hours and value per stage owner in [from, to), priced two ways:
# present-day (snapshot %(snapshot)s) and historical (per interval, the latest snapshot of the same
# provider/region/purchase option published at or before the interval's start; none = unpriced).
IDLE_VALUE_BY_OWNER = """
WITH sel AS (
  SELECT provider, region, purchase_option, currency
  FROM pricing_snapshots WHERE snapshot_id = %(snapshot)s
), iv AS (
  SELECT i.stage_owner, i.gpus, i.gpu_model, i.started_at,
         greatest(i.started_at, %(from)s) AS s,
         least(coalesce(i.ended_at, %(now)s), %(to)s) AS e,
         CASE WHEN o.objective IS NULL OR i.stage_entered_at IS NULL THEN NULL
              ELSE greatest(i.started_at, i.stage_entered_at + o.objective, %(from)s) END AS os
  FROM capacity_intervals i
  LEFT JOIN stage_objectives o ON o.stage = i.stage AND o.policy_version = i.policy_version
  WHERE i.category = 'IDLE'
    AND least(coalesce(i.ended_at, %(now)s), %(to)s) > greatest(i.started_at, %(from)s)
), hours AS (
  SELECT stage_owner, gpu_model, started_at,
         gpus * extract(epoch FROM e - s) / 3600 AS idle_h,
         CASE WHEN os IS NULL THEN NULL
              ELSE gpus * extract(epoch FROM greatest(e - os, interval '0')) / 3600 END AS overdue_h
  FROM iv
), valued AS (
  SELECT h.*, now_r.per_gpu_hour_usd AS now_rate, hist_r.per_gpu_hour_usd AS hist_rate
  FROM hours h
  LEFT JOIN gpu_model_aliases a ON a.raw_model = h.gpu_model
  LEFT JOIN pricing_rates now_r
         ON now_r.snapshot_id = %(snapshot)s AND now_r.gpu_model = a.canonical
  LEFT JOIN LATERAL (
    SELECT p.snapshot_id
    FROM pricing_snapshots p
    JOIN sel ON p.provider = sel.provider AND p.region = sel.region
            AND p.purchase_option = sel.purchase_option AND p.currency = sel.currency
    WHERE p.publication_date <= h.started_at
    ORDER BY p.publication_date DESC, p.captured_at DESC, p.snapshot_id DESC
    LIMIT 1
  ) hs ON true
  LEFT JOIN pricing_rates hist_r
         ON hist_r.snapshot_id = hs.snapshot_id AND hist_r.gpu_model = a.canonical
)
SELECT stage_owner,
       sum(idle_h) AS idle_gpu_hours,
       sum(idle_h * now_rate) AS idle_usd,
       coalesce(sum(idle_h) FILTER (WHERE now_rate IS NULL), 0) AS idle_unpriced_gpu_hours,
       sum(idle_h * hist_rate) AS idle_usd_historical,
       coalesce(sum(idle_h) FILTER (WHERE hist_rate IS NULL), 0)
         AS idle_unpriced_gpu_hours_historical,
       sum(overdue_h) AS overdue_gpu_hours,
       sum(overdue_h * now_rate) AS overdue_usd,
       coalesce(sum(overdue_h) FILTER (WHERE now_rate IS NULL), 0) AS overdue_unpriced_gpu_hours,
       sum(overdue_h * hist_rate) AS overdue_usd_historical,
       coalesce(sum(overdue_h) FILTER (WHERE hist_rate IS NULL), 0)
         AS overdue_unpriced_gpu_hours_historical,
       coalesce(sum(idle_h) FILTER (WHERE overdue_h IS NULL), 0) AS idle_gpu_hours_no_objective
FROM valued
GROUP BY stage_owner
"""

# Present-day value per hour of the GPUs idle now, and the idle GPUs it cannot price.
IDLE_VALUE_PER_HOUR_NOW = """
SELECT sum(i.gpus * r.per_gpu_hour_usd) AS usd_per_hour,
       coalesce(sum(i.gpus) FILTER (WHERE r.per_gpu_hour_usd IS NULL), 0) AS unpriced_gpus,
       coalesce(sum(i.gpus), 0) AS idle_gpus
FROM capacity_intervals i
LEFT JOIN gpu_model_aliases a ON a.raw_model = i.gpu_model
LEFT JOIN pricing_rates r ON r.snapshot_id = %(snapshot)s AND r.gpu_model = a.canonical
WHERE i.ended_at IS NULL AND i.category = 'IDLE'
"""
