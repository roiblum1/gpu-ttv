# Grafana: dashboards and data sources as code

Slice 1 (ADR-0001): the Queues view of E03 and the Evidence view of E18, read from the TTV ledger (`migrations/`). Slice 4 adds AWS-equivalent value in USD to Queues (ADR-0003).

```
grafana/
  provisioning/datasources/ttv.yaml   Postgres uid ttv-postgres, Prometheus uid ttv-prometheus
  provisioning/dashboards/ttv.yaml    file provider -> folder "GPU Time-to-Value" (uid gpu-ttv)
  dashboards/ttv-queues.json          uid ttv-queues   "Queues — where GPUs are stuck"
  dashboards/ttv-evidence.json        uid ttv-evidence "Evidence & monitoring health"
```

Only built-in panels are used (stat, bar gauge, table, time series, row). There are no plugins, external fonts, CDNs or remote images, so the files work offline as they are.

## Install

Mount `provisioning/datasources/` and `provisioning/dashboards/` under Grafana's provisioning directory (default `/etc/grafana/provisioning/`). Mount `dashboards/` at `/var/lib/grafana/dashboards/gpu-ttv` (the provider's `options.path`). Grafana reads these variables when it starts:

| Variable | Meaning |
| --- | --- |
| `TTV_PG_HOST` | `host:port` of the ledger Postgres |
| `TTV_PG_DB`, `TTV_PG_USER`, `TTV_PG_PASSWORD` | database, user and password (the password goes to `secureJsonData`) |
| `TTV_PG_SSLMODE` | `disable`, `require`, `verify-ca` or `verify-full`. **Set it explicitly** (`verify-full` in production). When it is unset, the driver default connected without TLS in testing. |
| `TTV_PG_SSLROOTCERT` | path to the CA bundle inside the Grafana container for `verify-ca`/`verify-full`; may be empty otherwise |
| `TTV_PROM_URL` | Prometheus/Thanos URL for the engine's `/metrics` (no slice-1 panel uses it yet) |

The Grafana database user needs only `SELECT` on the ledger tables, including `gpu_model_aliases`, `pricing_snapshots`, `pricing_rates` and `pricing_selection` from `migrations/0002_pricing.sql` (validated with a SELECT-only role for slice 1). Dashboards are read-only in the UI (`allowUiUpdates: false`). To change one, edit the JSON and keep its `uid` and panel `id`s stable. For air-gapped Grafana, also set `GF_ANALYTICS_REPORTING_ENABLED=false`, `GF_ANALYTICS_CHECK_FOR_UPDATES=false`, `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false`, `GF_NEWS_NEWS_FEED_ENABLED=false` and `GF_SECURITY_DISABLE_GRAVATAR=true`.

## Reading the dashboards

- **Now vs period.** "Now" panels read open intervals (`ended_at IS NULL`), i.e. the latest engine scan; "Data as of" and "Data age" show how old that is. Period panels clip every interval to the time range and end open intervals at `now()`.
- **Overdue** = the part of an IDLE interval after `stage_entered_at + objective`, joined on the interval's own `(stage, policy_version)` in `stage_objectives` (AGENT.md §20.5, §20.11). If an interval has no objective, its idle hours appear in "Idle GPU-h, no objective" and are never counted as 0 overdue.
- **Lower bounds.** When `stage_entered_lower_bound` is true, times, days and GPU-hours display as `≥ X`.
- **Unknown is never zero or green.** UNKNOWN GPUs are purple and kept separate. Panels with no evidence show "No data" or "never", not 0. Colours: green = productive/healthy, orange = idle/needs attention, red = overdue/problem, blue = NOT_CHARGED (approved exclusion), purple = unknown.
- **Filters.** `stage_owner` and `site` are multi-select. "All" sends `'__all__'`, which means no filter, so rows with no owner or no site are kept. Each panel's description lists the filters it honours. UNKNOWN, PRODUCTIVE and NOT_CHARGED stats ignore `stage_owner`, because those GPUs have no stage owner.
- Next-action annotations are display only (latest unexpired row per asset). They never change an owner or a total.
- **Value (USD).** Panels 16-20 and the USD columns of panel 12 show the AWS On-Demand equivalent (Linux, shared tenancy, us-east-1): GPU-hours × the per-GPU rate of each GPU's model, joined `gpu_model` → `gpu_model_aliases` → `pricing_rates`. It is a comparison, **not a bill, not our cost and not a loss**, and is never added to other money views (AGENT.md §10, ADR-0003).
  - `Price snapshot (USD)` (`$snapshot`) picks the present-day snapshot. It defaults to the engine's selection (`pricing_selection`: `TTV_PRICING_SNAPSHOT`, else the latest published). The stat shows the snapshot's region and publication date.
  - "Historical" columns value each interval at the snapshot in force when it started, so importing a newer snapshot never changes them. Intervals older than every snapshot are unpriced historically.
  - **Unpriced is never $0.** GPUs whose model has no rate are counted in "Unpriced idle GPUs now" (purple), listed with the reason in "Unpriced idle GPUs now, by model", and shown as unpriced GPU-h beside every USD column. A USD cell reading "unpriced" means nothing in that row could be priced. The value-per-hour stat excludes unpriced GPUs, so it is a lower bound whenever that count is above 0.
  - Dollars use the unit `prefix:$` so they are exact, not abbreviated to K/M.
- **GPU use (E12, `ttv-gpu-use`).** Each GPU falls in one use class, taken from its open interval:
  - *Useful work*: PRODUCTIVE, i.e. a Run:ai workload, or serving with genuine requests.
  - *Allocated, inactive*: PRODUCTIVE but flagged `allocated_idle`. The ledger still counts these GPUs as productive.
  - *Waste candidate*: `NO_REAL_DEMAND`, `LOW_USE`, `OTHER_WORKLOAD`, `NO_WORKLOAD`.
  - *Constrained or failed*: any other IDLE GPU on a matched node (`WORKLOAD_NOT_READY`, `GPU_MISSING`, platform or node not ready).
  - *Use unknown*: in a measured cluster, but the evidence is missing (`TRAFFIC_UNOBSERVED`, `HOLDER_UNRESOLVED`, `NODE_NOT_FOUND`, `CLUSTER_UNREACHABLE`, a `NODE_CLAIM` conflict).
  - *Not measured* (purple): the GPU is not in a target cluster the engine reads. "Why GPUs are not measured" gives the reasons.

  The useful-work share is taken of measured GPUs. The not-measured share is taken of all GPUs, so the two are never added. "From allocation to useful work" shows that a held GPU or a loaded model is not delivered value. The `cluster` filter uses the interval's cluster, else the cluster named in its evidence or its `NODE_CLAIM` conflict. R1 has no source for per-GPU activity, memory pressure or SLO compliance, so those panels name the missing input instead of a number.
- **Asset (`ttv-asset`).** Open it as `/d/ttv-asset?var-asset_id=<asset>`. Without that, it shows the asset with the most idle GPUs. The page shows identity, current buckets with their evidence in plain words, milestones (`≥` = lower bound), open identity conflicts, next-action annotations (display only), the latest collector record and a category timeline.
  - "Engine explanation" opens `${ttv_api}/explain/<asset>`. `ttv_api` is a hidden textbox, default `http://ttv-engine:8080`, and must be a URL the viewer's browser can reach, e.g. the engine's Route. To change it, edit its default in `ttv-asset.json` or add `&var-ttv_api=<url>` to the page URL.
  - The panels on `milestones`, `identity_conflicts`, `next_action_annotations` and the collector tables run their query (through `query_to_xml`) only when the Grafana role can read the table. Otherwise they show a "Not available" row, not an error. Grant `SELECT` on `collector_reads` and `collector_servers` (migration 0003) as well.

Assumptions the SQL makes about the engine:
1. All intervals of one stage episode carry the same `stage_entered_at`.
2. Scan kinds are whatever `scan_runs.kind` contains.
3. The source-health State column assumes the default `unknown_grace_scans = 2`.

## Validation record (fixture-tested, not production evidence)

2026-10-30, against Postgres 17 with `migrations/0001_init.sql` and a synthetic fixture:
- Every panel query ran through `/api/ds/query` with no errors on **Grafana 12.4.14** (`docker.io/grafana/grafana@sha256:ae509d9ae8c1021ca32ec370a9ca975d40165eda9fccd1e1102d317ff8787cd7`) and **Grafana 13.2.5** (`@sha256:9f7aa1e1e3246aade5792d35a738049f7f32f89cd51d1956cf426a4e86268a84`).
- `/api/search` lists both dashboards in the folder.
- Both dashboards were rendered in a browser with All and with specific filter values.
- AGENT.md §17, through Grafana's own macros: 8 GPUs idle for 3 h with a 1 h objective gave 24 idle and 16 overdue GPU-h. The same interval clipped to 00:30–02:30 gave 16 idle and 12 overdue. 6 idle GPUs for 2 h beside 2 productive ones gave 12 idle GPU-h.

To re-check, start Postgres 17 and apply the migration and a fixture. Run Grafana with the mounts above. Then query `/api/dashboards/uid/ttv-queues` and post each panel's target to `/api/ds/query`, substituting `$site`/`$stage_owner` with `'__all__'` as the browser would.

2026-10-31, slice 4 (pricing), `lab-tested` (not production evidence): `ttv_lab_pricing` (lab ledger from the sandbox, migration 0002, the us-east-1 snapshot published 2026-10-29):
- Every panel and variable query of both dashboards ran with macros substituted (`$__timeFrom()`/`$__timeTo()`, `$site`/`$stage_owner` = All, `$snapshot` = the snapshot id) with no errors.
- A throwaway Grafana **12.4.14** (same digest as above) provisioned from these files against that database: all 15 Queues queries passed through `/api/ds/query` with All, with `stage_owner=Redbull` and with an empty snapshot; the Evidence dashboard's queries had no errors either. The page was rendered in a browser: idle value $1865.30 per hour (snapshot us-east-1, published 2026-10-29 17:56 UTC) and 178 unpriced idle GPUs (MI300X, RTX A6000, A16) with reasons.
- The browser check found and fixed three problems: a stat's field regex matches the display name (so it went blank), `currencyUSD` abbreviated $1,261 to "$1K", and long headers were truncated.
