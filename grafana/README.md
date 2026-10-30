# Grafana: dashboards and data sources as code

Slice 1 (ADR-0001): the Queues view of E03 and the Evidence view of E18, read from the TTV ledger (`migrations/0001_init.sql`).

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

The Grafana database user needs only `SELECT` on the ledger tables (validated with a SELECT-only role). Dashboards are read-only in the UI (`allowUiUpdates: false`). To change one, edit the JSON and keep its `uid` and panel `id`s stable. For air-gapped Grafana, also set `GF_ANALYTICS_REPORTING_ENABLED=false`, `GF_ANALYTICS_CHECK_FOR_UPDATES=false`, `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false`, `GF_NEWS_NEWS_FEED_ENABLED=false` and `GF_SECURITY_DISABLE_GRAVATAR=true`.

## Reading the dashboards

- **Now vs period.** "Now" panels read open intervals (`ended_at IS NULL`), i.e. the latest engine scan; "Data as of" and "Data age" show how old that is. Period panels clip every interval to the time range and end open intervals at `now()`.
- **Overdue** = the part of an IDLE interval after `stage_entered_at + objective`, joined on the interval's own `(stage, policy_version)` in `stage_objectives` (AGENT.md §20.5, §20.11). If an interval has no objective, its idle hours appear in "Idle GPU-h, no objective" and are never counted as 0 overdue.
- **Lower bounds.** When `stage_entered_lower_bound` is true, times, days and GPU-hours display as `≥ X`.
- **Unknown is never zero or green.** UNKNOWN GPUs are purple and kept separate. Panels with no evidence show "No data" or "never", not 0. Colours: green = productive/healthy, orange = idle/needs attention, red = overdue/problem, blue = NOT_CHARGED (approved exclusion), purple = unknown.
- **Filters.** `stage_owner` and `site` are multi-select. "All" sends `'__all__'`, which means no filter, so rows with no owner or no site are kept. Each panel's description lists the filters it honours. UNKNOWN, PRODUCTIVE and NOT_CHARGED stats ignore `stage_owner`, because those GPUs have no stage owner.
- Next-action annotations are display only (latest unexpired row per asset). They never change an owner or a total.

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
