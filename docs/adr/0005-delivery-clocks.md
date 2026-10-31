# ADR-0005: Delivery clocks, lower bounds and insufficient history (E02)

- **Status:** proposed 2026-10-31, awaiting the user's review of the Delivery page.
- **Context:** AGENT.md §13 "Are we getting faster?" (requirements 1-8), §20.5 "Lower bounds", §20.11, §0.6 (E02 in R1, E07 in R2); ADR-0002 "Stage clocks" and "Milestones".
- **Code:** `grafana/dashboards/ttv-delivery.json` (SQL only, no view or migration), `config/panel-contracts/ttv-delivery.yaml`, `tests/test_delivery_sql.py`.

## Clocks

Eight clocks are kept apart so that one team's delay is never booked to another. None is a sum of other clocks' percentiles.

| Clock | Start | End | Sample |
| --- | --- | --- | --- |
| First record → GPU-ready | first record (below) | `gpu_ready` milestone | one per server |
| GPU-ready → workload running | `gpu_ready` | `workload_running` (pod start time) | one per server |
| Workload → first genuine request | `workload_running` | `first_genuine_request` | one per server |
| End-to-end | first record | `first_genuine_request` of the same server | one per server |
| DC, Redbull, POPAI, Serving stage | `stage_entered_at` of the episode | last `ended_at` of the episode | one per stage episode |

- **First record** = the earlier of `in_serverscan` (server-scan's `created_at`, exact) and `first_seen` (the engine's first sighting). Physical receipt or acceptance needs procurement data (R3).
- **Stage episode** = all IDLE intervals with the same `(asset_id, stage, stage_entered_at)` (README assumption 1). It is complete when none is open and an interval of the same asset starts at its last `ended_at` in a **later** stage or PRODUCTIVE. Episodes that went back to an earlier stage (rework), became NOT_CHARGED or UNKNOWN, or left the ledger are not completions: they are counted as excluded.
- A negative milestone difference (a pod started a few minutes before the scan that first saw the GPUs ready) is recorded as 0: it is below the scan's resolution.

## Exact, lower-bound and unusable samples

The engine marks `gpu_ready`, `workload_running` and `first_genuine_request` as `lower_bound` every time, because the classifier is pure and cannot see history (ADR-0002). The ledger can: it already dates a stage entry exactly when the asset came from another staged state (`ledger.py` `_stage_clock`). The page applies the same evidence to milestones. A milestone counts as **exact** when:

| Milestone | Exact when |
| --- | --- |
| `in_serverscan` | always (its own flag is false) |
| `first_seen` | the engine's previous scan (`scan_runs`, kind `engine`) has status `ok`, i.e. the asset appeared between two complete scans |
| `gpu_ready` | an IDLE interval of the asset in DC, Redbull or POPAI (not `GPU_MISSING`) ended exactly at the milestone's scan |
| `workload_running` | `gpu_ready` is exact, or the pod started after GPU-ready was first seen |
| `first_genuine_request` | an IDLE interval of the asset ended at the milestone's scan and no UNKNOWN interval of the asset covered the moment before (e.g. `TRAFFIC_UNOBSERVED`) |

This relies on the engine stamping milestones, `scan_runs.started_at` and interval boundaries with the same scan time (true at `engine.py` today).

Each sample is then:
- **exact**: both ends exact;
- **lower bound (`≥`)**: exact end, start that may have been earlier (a stage clock with `stage_entered_lower_bound`, or a first record that is only a sighting). The true duration is at least the observed one;
- **unusable**: the end is not exact (the milestone was already true at first sight, so the true duration could be shorter), or the episode did not end in a hand-over. These are never used in a percentile and are counted as "excl.".

## Percentiles and insufficient history

Decision: **exclude bounded samples and say how many, with a `≥` fallback.** For each clock and period:
1. at least `min_n` (5) exact samples → p50 and p90 of the exact samples; lower-bound samples are excluded and counted;
2. otherwise, at least 5 exact plus lower-bound samples → p50 and p90 of those, shown as `≥` (each sample is at most its true value, so each percentile is at most the true one);
3. otherwise → "insufficient history (n = …)", never a number.

A change between periods is computed only from exact percentiles with at least 5 samples in both periods: "4.0 d shorter / 33.3% shorter", with the percentage suppressed on a zero baseline (§13 #8). The comparison is the last `$period` complete UTC days before the end of the time range against the `$period` days before that (default 28).

The verdict says "Quicker:" (green) only when p50 is shorter, p90 is not longer, no open episode is overdue or possibly overdue, and there were no fewer completions than before. Otherwise a quicker comparison reads "Quicker on completed work only" (orange). Every verdict carries the backlog now.

## Cohorts

Servers with an exact first record are grouped by UTC intake week. A cell (milestone by 1, 7, 14 or 30 days) is "not matured" until the youngest server in the cohort is that old. A server counts as reached when the milestone was recorded by that age; as not reached when the ledger held it only in IDLE or PRODUCTIVE intervals at that age; otherwise its state is unknown. Unknowns count as not reached, so the share is a minimum and shows `≥`.

## Validation (2026-10-31; not production evidence)

- `fixture-tested`: `tests/test_delivery_sql.py` runs the panel queries from the JSON on Postgres 17 with synthetic ledgers: 12 d → 8 d reads "4.0 d shorter / 33.3% shorter"; 2 exact + 4 lower-bound samples read "≥ 3.5 d"; a GPU-ready already true at first sight is excluded; fewer than 5 samples read "insufficient history"; the verdict is qualified while the backlog is overdue; cohort cells; an empty ledger shows no value.
- `fixture-tested`: a synthetic 100-day ledger (190 servers) through a throwaway Grafana 12.4.14: all 448 panel-query variants (filters, periods, clocks, 90-day and 6-hour ranges) ran without error through `/api/ds/query`, and the page was screenshotted at 1440 and 2560 px wide.
- `lab-tested`: the same 448 variants against the lab ledger `ttv_lab2` (about 4.7 h of history), read-only; the page shows "insufficient history" for every percentile and `≥` backlog ages, as intended.

## Consequences

- With hours of history (the lab), almost every clock says "insufficient history" and DC clocks are lower bounds by construction (the collector's first sighting is not a receipt date). That is the intended reading, not a defect.
- If the engine ever sets milestone `lower_bound = false` itself, the page uses that flag directly; the evidence rules above become redundant but stay correct.
- Known simplification: the ledger resumes a stage episode when an asset returns to the same stage after a PRODUCTIVE or UNKNOWN gap (`_stage_clock` uses the last staged interval). Such an episode's duration includes the gap, and it is open again until the asset leaves the stage.
