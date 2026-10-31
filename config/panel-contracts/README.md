# Panel contracts

One YAML file per dashboard, named after its UID (`ttv-queues.yaml` for `grafana/dashboards/ttv-queues.json`). Each non-row panel has exactly one entry, keyed by its Grafana panel `id`, which must stay stable (AGENT.md §13 "A contract for every executive panel"). The question map that ties E01–E18 to these entries is `docs/executive-question-map.md`. `tests/test_panel_contracts.py` checks the files against the dashboards.

```yaml
dashboard: ttv-queues            # the dashboard UID
panels:
  - id: 7                        # Grafana panel id
    title: Idle GPUs now, by stage owner     # must equal the panel title
    question: E03                # E01..E18, or "support" for scope/as-of/navigation panels
    status: built                # built | missing_input
    audience: CEO, stage owners  # who reads it
    decision: Which handoff queue to unblock first.
    definition: >-
      GPUs in open IDLE intervals, summed per stage_owner.
    unit: GPUs
    numerator: sum(capacity_intervals.gpus) where category = IDLE and ended_at IS NULL
    denominator: null            # or the population a share is taken of
    population: Every GPU asset in the ledger (server-scan and the profile-less collector).
    window: now                  # now | selected range | <explicit window>
    filters: [stage_owner, site] # dashboard variables honoured
    exclusions: UNKNOWN, PRODUCTIVE and NOT_CHARGED GPUs.
    query: grafana/dashboards/ttv-queues.json#7      # where the exact query lives
    lineage: [capacity_intervals.gpus, capacity_intervals.stage_owner, assets.site]
    freshness: Latest engine scan; "Data as of" shows its age.
    baseline: null               # comparison baseline, if any
    direction: lower_is_better   # lower_is_better | higher_is_better | neutral
    thresholds: null
    uncertainty: >-
      Stage clocks that began before observation are lower bounds (≥).
    missing_data: >-
      No open IDLE interval shows "No idle GPUs"; a stale source turns GPUs UNKNOWN, never idle.
    value_kind: observed         # observed | derived | forecast | scenario | benchmark | business_input | assumption
    gate: core                   # core | history | business_input
    privileges: SELECT on capacity_intervals, assets
    cardinality: one row per stage owner (≤ 4)
    drilldown: ttv-queues#14     # panel or dashboard it opens into, or null
    action: Ask the stage owner to clear the longest waits (Queues, "Longest waits").
    fixture: tests/test_dashboards_sql.py             # fixture or test that exercises it
    expected: 6 idle GPUs for Redbull with the scenario fixture.
```

Required keys: `id`, `title`, `question`, `status`, `audience`, `decision`, `definition`, `unit`, `population`, `window`, `filters`, `query`, `lineage`, `freshness`, `direction`, `missing_data`, `value_kind`, `gate`. The others may be `null` but must be present.

A `missing_input` panel is a `text` panel. Its contract names the missing inputs in `definition` and `missing_data`, and uses `gate: history` or `gate: business_input`.
