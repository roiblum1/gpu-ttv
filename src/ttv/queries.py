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
