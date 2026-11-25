# Grafana: dashboards and data sources as code

Six pages for senior managers, read from the TTV ledger (`migrations/`): a summary and one waste page per team ([ADR-0009](../docs/adr/0009-five-teams-and-manager-dashboards.md)). They answer two questions: how many GPUs are wasted right now, and how many GPU-hours were wasted in the selected period, with what that waste would cost at AWS prices and which GPU models it sits on. They use plain words; the only money is the AWS On-Demand price (ADR-0003), a comparison and not our bill. On 2026-11-25 they replaced the ten earlier pages (Home, Queues, Delivery, GPU use, Asset, Evidence and four missing-input pages).

```
grafana/
  provisioning/datasources/ttv.yaml   Postgres uid ttv-postgres, Prometheus uid ttv-prometheus
  provisioning/dashboards/ttv.yaml    file provider -> folder "GPU Time-to-Value" (uid gpu-ttv)
  provisioning/alerting/              alert rules and inert notification routing ("Alerts" below)
  dashboards/ttv-home.json            uid ttv-home     "GPU waste: summary"
  dashboards/ttv-samurai.json         uid ttv-samurai  "GPU waste: Samurai"
  dashboards/ttv-redbull.json         uid ttv-redbull  "GPU waste: Redbull"
  dashboards/ttv-popai.json           uid ttv-popai    "GPU waste: POPAI"
  dashboards/ttv-apply.json           uid ttv-apply    "GPU waste: Apply"
  dashboards/ttv-ml.json              uid ttv-ml       "GPU waste: Machine Learning"
```

Only built-in panels are used (stat, bar gauge, bar chart, table, time series). There are no plugins, external fonts, CDNs or remote images, so the files work offline as they are. The Helm chart globs `dashboards/*.json` through `deploy/helm/gpu-ttv/files/grafana` (a link to this folder), so it renders one ConfigMap per page with no chart change.

## Install

Mount `provisioning/datasources/` and `provisioning/dashboards/` under Grafana's provisioning directory (default `/etc/grafana/provisioning/`). Mount `dashboards/` at `/var/lib/grafana/dashboards/gpu-ttv` (the provider's `options.path`). For a sidecar-based Grafana on OpenShift, the Helm chart renders these same files as labelled ConfigMaps instead (`grafana.*` in `deploy/helm/gpu-ttv/values.yaml`, ADR-0007). Grafana reads these variables when it starts:

| Variable | Meaning |
| --- | --- |
| `TTV_PG_HOST` | `host:port` of the ledger Postgres |
| `TTV_PG_DB`, `TTV_PG_USER`, `TTV_PG_PASSWORD` | database, user and password (the password goes to `secureJsonData`) |
| `TTV_PG_SSLMODE` | `disable`, `require`, `verify-ca` or `verify-full`. **Set it explicitly** (`verify-full` in production). When it is unset, the driver default connected without TLS in testing. |
| `TTV_PG_SSLROOTCERT` | path to the CA bundle inside the Grafana container for `verify-ca`/`verify-full`; may be empty otherwise |
| `TTV_PROM_URL` | Prometheus/Thanos URL for the engine's `/metrics` (no page uses it yet) |

The Grafana database user needs only `SELECT`. The pages read `capacity_intervals`, `assets`, `scan_runs` and `workloads_now` (`migrations/0007_pod_requests_and_workloads_now.sql`), and for the AWS price and the GPU model names `gpu_model_aliases`, `pricing_rates`, `pricing_selection` and `pricing_snapshots` (`migrations/0002_pricing.sql`). Dashboards are read-only in the UI (`allowUiUpdates: false`). To change one, edit the JSON and keep its `uid` and panel `id`s stable. To open the summary first, set `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/gpu-ttv/ttv-home.json`. For air-gapped Grafana, also set `GF_ANALYTICS_REPORTING_ENABLED=false`, `GF_ANALYTICS_CHECK_FOR_UPDATES=false`, `GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false`, `GF_NEWS_NEWS_FEED_ENABLED=false` and `GF_SECURITY_DISABLE_GRAVATAR=true`.

## The pages

| Order | UID | Title | Panels |
| --- | --- | --- | --- |
| 1 | `ttv-home` | GPU waste: summary | GPUs wasted now; GPU-hours wasted; cost of waste at AWS prices (period); costing now ($ per hour, about $ a day); five team tiles (Samurai, Redbull, POPAI, Apply, Machine Learning) with each team's cost for the period, each opening its page; where every GPU is now; data as of; GPUs wasted over time, by team; GPUs by model now |
| 2 | `ttv-samurai` | GPU waste: Samurai | the team panels below, plus "Drawing power" (#8: GPUs no pod holds that draw power); table: servers with empty GPUs |
| 3 | `ttv-redbull` | GPU waste: Redbull | the team panels; table: servers not ready yet |
| 4 | `ttv-popai` | GPU waste: POPAI | the team panels; table: servers whose GPU software is not ready |
| 5 | `ttv-apply` | GPU waste: Apply | the team panels; table: every served model, wasted or not |
| 6 | `ttv-ml` | GPU waste: Machine Learning | the team panels; table: every machine learning job, wasted or not |

Every team page has the same seven panels, with the same ids: #1 GPUs wasted now, #2 GPU-hours wasted, #6 cost of waste at AWS prices, #3 why GPUs are wasted now (reasons in plain words), #7 wasted GPUs by model, #4 wasted GPUs over time by reason, #5 the table (with a GPU model column and a money column). Samurai's page also has #8 "Drawing power". Home's panel ids: #1, #2, #11 cost, #12 costing now, #5–#9 tiles, #3 where every GPU is, #4 data as of, #10 trend, #13 GPUs by model. Samurai comes first on the summary and in the links bar because the user cares most about healthy GPUs with nothing on them.

## Design conventions (every page)

These rules keep the pages readable for people who are not technical. A page that breaks one needs a reason in its panel contract. `tests/test_dashboards_static.py` checks the ones a test can check.

- **Six pages, stable UIDs.** Every page has the tags `gpu-ttv` and `gpu-waste` and the same `links` bar in the order of the table above (titles Summary, Samurai, Redbull, POPAI, Apply, Machine Learning), each link with `includeVars: true` and `keepTime: true`.
- **Waste is IDLE, and pages select by stage.** Each team page reads `capacity_intervals` rows with its `stage` (`EMPTY` Samurai, `PROVISIONING` Redbull, `GPU_PLATFORM` POPAI, `SERVING` Apply, `MACHINE_LEARNING` Machine Learning), never by team name. Team names shown in labels and series come from the latest `stage_owner` the ledger records for that stage (the policy's `teams`), so renaming a team never empties a page; the tile titles and page titles are fixed text. Order is always Samurai, Redbull, POPAI, Apply, Machine Learning.
- **Money is the AWS price, and nothing else** (the user, 2026-11-26). Every dollar figure is what the same GPUs would cost to rent on AWS On-Demand, us-east-1, at **one** snapshot: the engine's choice in `pricing_selection`, else the latest by publication date then capture time (ADR-0003's present-day valuation; no historical valuation, no `snapshot` variable). The join is `capacity_intervals.gpu_model → gpu_model_aliases.raw_model → canonical → pricing_rates (snapshot_id, gpu_model).per_gpu_hour_usd`, in a shared `snap` CTE. Money panels say "at AWS prices", and their description says it is a comparison, not our bill; labels name the price list's date and region. No other currency, energy or internal cost appears. `tests/test_dashboards_static.py` checks all of this.
- **Unpriced is never $0.** Wasted GPU-hours whose model has no rate make a figure "at least" (stats) or `≥` (tiles and tables), with the unpriced GPU-hours or GPUs in the label. When nothing is priced the value is empty ("No price", "no AWS price"); with no snapshot imported it reads "no AWS price list loaded" ("no price list" in tables). Nothing wasted is a real $0.
- **Rounding.** Labels and tables: whole dollars from $100 (`$9,624`), cents below (`$48.00`). Stats use the `prefix:$` unit, which has no thousands separator: whole dollars for the period costs, cents for "Costing now" ($ per hour).
- **GPU models** are shown by their canonical name (`gpu_model_aliases`), else as reported, else "Unknown model", always without "NVIDIA ". A GPU with an unknown model is never dropped.
- **Plain words.** No raw reason or category code reaches a viewer: the SQL maps each one to a label (table below). Every panel has a description of one to a few sentences, shown by the panel's info icon.
- **Few panels, big numbers.** The headline stats use value size 56; tiles 40; one number per stat, with a short label under it (`displayName: ${__data.fields.label}`).
- **Variables.** Only the time range (default the last 7 days) and an optional `cluster` filter (multi, All = `'__all__'`). The cluster of a row is `coalesce(cluster, evidence->>'cluster', evidence->>'cluster_name', '(no cluster)')`; `workloads_now` rows use their own `cluster`.
- **Unknown is never zero.** UNKNOWN GPUs are "No data": a purple bar on the summary, a dashed purple line on its trend, and a count in the headline labels. They are never counted as wasted and never shown as 0. Before the first scan, or for a cluster filter that matches no GPU, values are empty ("no data yet", "no scan yet"), not 0. After that, a team with nothing wasted shows a real 0.
- **Lower bounds** display as `≥ X` in tables (a stage that began before observation, an incomplete request count), and as "at least" in a stat's label (a range that starts before the first scan).
- **Colours.** Orange is waste, green is in use, blue is in repair, purple is no data, and the text colour is neutral (not measured, excluded). On the Apply and Machine Learning tables, wasted rows have a light orange background and no-data rows a light purple one; the status is always written too.
- **Over-time series** sample open intervals on a grid of at least 5 minutes, never by summing interval rows, and start at the first ok or partial engine scan (no evidence before it).
- **Size.** Validate at 1440×900 with Grafana's side menu docked (the grid is then about 1,108 px, 46 px per unit). A panel title gets about `46 × w − 106` px. Tables do not scroll sideways: every table wraps its header text, each column has a `custom.width` and exactly one text column a `custom.minWidth`. `tests/test_dashboard_layout.py` checks titles, overrides and table widths.
- **Panel contracts.** Every panel has a contract in `config/panel-contracts/<uid>.yaml`, checked by `tests/test_panel_contracts.py` (schema in `config/panel-contracts/README.md`). `docs/executive-question-map.md` maps E01–E18 to the pages.

### Plain words

| Stage (team) | Reason code | Shown as |
| --- | --- | --- |
| PROVISIONING (Redbull) | NOT_IN_CLUSTER, IN_MCE_INVENTORY, NODE_NOT_READY, NODE_UNSCHEDULABLE, HARDWARE_CRITICAL, BMC_UNREACHABLE | Not in a cluster yet; Waiting in cluster inventory; Node not ready; Node switched off for scheduling; Hardware problem; Management controller unreachable |
| GPU_PLATFORM (POPAI) | GPU_PLATFORM_NOT_READY, GPU_MISSING | GPU software not ready; GPUs missing |
| EMPTY (Samurai) | NO_WORKLOAD | Empty: nothing running on it |
| SERVING (Apply) | LOW_USE, NO_REAL_DEMAND, MODEL_NOT_READY | Barely used; No requests; Model not starting |
| MACHINE_LEARNING (Machine Learning) | LOW_GPU_UTILIZATION, WORKLOAD_NOT_READY | GPUs barely busy; Job not starting |
| history (policies before ADR-0009) | MAINTENANCE, OTHER_WORKLOAD as IDLE | In repair (before 25 Nov 2026); Other workload (before 25 Nov 2026) |
| no data (UNKNOWN, no stage) | POWER_WITHOUT_POD | Drawing power, no pod |
| any other idle reason | | Other reason |

Where every GPU is now: PRODUCTIVE is "In use", IDLE "Wasted", NOT_CHARGED/MAINTENANCE "In repair", UNKNOWN "No data", NOT_CHARGED/OUTSIDE_TARGET_CLUSTERS "Not measured", any other NOT_CHARGED "Excluded".

Statuses on the Apply table: "Busy" (in use, requests in the last hour), "Quiet this hour" (in use, none in the last hour), "In use" (in use, last hour not read), "Barely used", "No requests", "Not starting", "No data". On the Machine Learning table: "Busy", "Barely busy", "Not starting", "No data".

## Reading the pages

- **Now vs period.** "Now" panels read open intervals (`ended_at IS NULL`), i.e. the latest engine scan; "Data as of" on the summary says how old it is. "GPU-hours wasted" sums `gpus ×` the overlap of each IDLE interval with the time range, with open intervals ending at `now()`. AGENT.md §17: 8 GPUs idle for 3 h are 24 GPU-hours, 16 when the range clips them to 2 h, and 6 of 8 GPUs idle for 2 h are 12.
- **The summary's headline labels** say how many GPUs are tracked and how many have no data, and how many GPU-hours in the range have no data.
- **Team tables (Samurai, Redbull, POPAI)** list one row per server and stage episode: server name, GPU model (`assets.gpu_model`), cluster, GPUs, reasons, "Waiting since" (`≥` when the stage began before observation, e.g. Redbull's clock from server-scan's `created_at`), "GPU-hours so far" (GPUs now × time waiting) and "Cost so far at AWS prices" (the same × the rate, with the same `≥`, plus `≥` when part of the server is unpriced), biggest first.
- **Money on the summary.** "Cost of waste at AWS prices" values the period's wasted GPU-hours; "Costing now" is the wasted GPUs × their rates, per hour, with about that × 24 a day in its label; each tile's label gives its team's cost for the period. "GPUs by model now" has one row per model, biggest fleet first, and an "All models" row last: GPUs (all tracked), in use, wasted, in repair, no data (these four add up to "Where every GPU is now"), the wasted share, the AWS price per GPU-hour, and what the wasted ones cost per hour.
- **Apply** lists every `workloads_now` row of kind `serving`: model and namespace (from `holder_ref` `namespace/Kind/name`), cluster, GPUs, requests in the last hour and in the last 24 h, requests per GPU per hour, and the status. A model is barely used below 1 request per GPU per hour over 24 h, or since it started if it is younger ("24 in 6 h" names the hours covered); a model with enough requests is still barely used when its GPUs averaged under 25% of their rated power over 24 h. "GPU power (24 h average)" shows that share (`gpu_power_avg`), blank when unread. `≥` means the history does not cover the window yet (`*_complete` false). Waste rows first, then no data, then models in use. It agrees with the ledger: the GPUs of its wasted rows add up to the page's "GPUs wasted now". "GPU model" joins the workload's `gpu_models`; "Waste per day at AWS prices" is the GPUs × rate × 24 of the workload's open ledger intervals (same cluster, holder and reason), on wasted rows only.
- **Machine Learning** lists every `workloads_now` row of kind `ml`: job, namespace, cluster, Run:ai or other, GPUs, the peak GPU activity in the last 6 h, the status, the GPU models and, on wasted rows, the waste per day at AWS prices. A job's GPUs are barely busy when, in the last 6 h, they were never above 5% busy and never drew more than 25% of rated power (power alone when busy is not measured). "GPU power (peak, 6 h)" shows the peak share (`gpu_power_peak`), blank when unread. Unresolved workloads (kind `unresolved`) are on neither table; they are "No data" on the summary.
- **GPU power** is a share of each GPU's rated power: the DCGM power limit when exported, else the engine's table by model (H100/H200 700 W, B200 1000 W, B300 1100 W, H100 PCIe 350 W, …). Below 25% a GPU is idle. A GPU no pod holds that drew at least 25% in the last 6 h is UNKNOWN (`POWER_WITHOUT_POD`), not Samurai's waste: Samurai's "Drawing power" counts these GPUs, and the summary counts them in "No data". Something we cannot see is using them; the platform team should look.
- **History** keeps the reasons it was recorded with: before ADR-0009, maintenance counted as Redbull's waste and empty GPUs as Serving's, so a range that spans 2026-11-25 mixes both.

Assumptions the SQL makes about the engine:
1. All intervals of one stage episode carry the same `stage_entered_at`.
2. `scan_runs.kind = 'engine'` with status `ok` or `partial` is a scan that wrote the ledger; `dry_run` scans are not.
3. A writing scan replaces all of `workloads_now`, so it never holds stale rows.

## Validation record (not production evidence)

- **2026-11-25, `fixture-tested`:** every panel and variable query of the six pages ran against Postgres 17 with all migrations and a synthetic fixture, through `tests/grafana_sql.py` (Grafana's macro and variable substitution, itself checked against Grafana 12.4.14 on 2026-10-31), with All and with one cluster. `tests/test_dashboards_sql.py` asserts the numbers. The pages have not yet been provisioned in a Grafana or viewed in a browser; the row highlight on the Apply and Machine Learning tables (`color-background` with `applyToRow`) is the part to check first.
- **2026-11-26, `fixture-tested`:** the AWS price and GPU model panels, against two synthetic snapshots (the selected one at $10 and $1 per GPU-hour with one unpriced model, and a newer unselected one at $20), §17's linear pricing ($24 for 24 GPU-hours at $1, $16 clipped), "no price" and "no price list", and per-model rows that add up to "Where every GPU is now".
- **Lessons kept from validating the earlier pages** (Grafana 12.4.14 and 13.2.5, 2026-10-30/31; those pages are gone): a stat's field regex matches the display name, so select the number with the query, not a regex; long headers truncate unless they wrap; every query must run with a SELECT-only role.

## Alerts

R1 alerts for monitoring blindness and stage-objective breaches (AGENT.md §20, §14). They are Grafana-managed rules that read the ledger, with no Prometheus dependency. The design is in ADR-0006; the runbooks and the rule contract table are in `docs/runbooks.md`.

```
grafana/provisioning/alerting/
  ttv-rules.yaml           6 rules in folder "GPU Time-to-Value alerts":
                           group ttv-data-quality (1m), group ttv-stage-objectives (5m)
  ttv-notifications.yaml   inert contact point, always-on mute timing, policy tree
                           grouped by kind, owner, alertname
```

| Rule | kind | owner | Fires when |
| --- | --- | --- | --- |
| TTV engine scans stale | data_quality | platform | No ok/partial engine scan for > 15 min (for 5m) |
| TTV source failing past grace | data_quality | platform | A server-scan, kube, Thanos or vLLM source failed ≥ 2 consecutive scans (for 5m) |
| TTV UNKNOWN share high | data_quality | platform | > 10% of ledger GPUs UNKNOWN (for 30m) |
| TTV UNKNOWN share growing | data_quality | platform | UNKNOWN share up > 5 points in 1 h (for 10m) |
| TTV node not found | data_quality | platform | `NODE_NOT_FOUND` GPUs in an observed cluster (for 30m) |
| TTV stage objective breached | stage_breach | the stage owner | Idle GPUs past their stage objective, one instance per owner: Samurai, Redbull, POPAI, Apply, Machine Learning (for 1h, keep firing 1h) |

### Install

- **Mount** `provisioning/alerting/` at `/etc/grafana/provisioning/alerting/`, next to the datasource and dashboard mounts above.
  - A malformed alerting file stops Grafana from starting. `uv run pytest tests/test_alert_rules.py` checks both files.
  - Alerting provisions before dashboards, which is why the rules have their own folder: a shared title would create a second "GPU Time-to-Value" folder.
- **Grants.** The rules read `scan_runs`, `schema_migrations`, `source_state`, `capacity_intervals`, `stage_objectives`, `assets` and `next_action_annotations`: grant `SELECT` on those as well as on the pages' tables. A missing grant shows as a `DatasourceError` or `DatasourceNoData` alert for the platform, never as a healthy rule.
- **Links.** `dashboard_url` is built from Grafana's `root_url` (`GF_SERVER_ROOT_URL`); set it so links are absolute. Data-quality alerts open the summary (`ttv-home`; the rule's panel is "Data as of" or "Where every GPU is now"). A stage breach opens its owner's page (`ttv-redbull`, `ttv-popai`, `ttv-samurai`, `ttv-apply` or `ttv-ml`), named by query B's `page` label from the stage of the owner's open intervals. The Asset page is gone, so there is no `asset_url`; the description names the oldest servers instead.
- **Runbooks.** `runbook_url` points at `https://github.com/roiblum1/gpu-ttv/blob/main/docs/runbooks.md#<rule uid>`. For the air-gapped install, rewrite that prefix to the internal Git mirror in `ttv-rules.yaml`, and change `RUNBOOK_BASE` in `tests/test_alert_rules.py` to match.
- **Shared Grafana.** `ttv-notifications.yaml` replaces the organisation's whole notification policy tree. In a Grafana that other teams use, mount only `ttv-rules.yaml` and add the two routes to the existing tree by hand.
- **Thresholds.** Thresholds copy engine settings: 300 s scans and `TTV_UNKNOWN_GRACE_SCANS=2`. Edit the rule when one of them changes. The UNKNOWN-share thresholds are provisional.

### Notification destinations (none configured)

Destinations stay unconfigured until the user supplies them (§14), and nothing is sent anywhere:

- `ttv-unconfigured` is the only contact point. It is a webhook to `http://127.0.0.1:1/…`, a closed port on Grafana's own loopback.
- Both TTV routes carry the mute timing `ttv-until-destinations-supplied` (00:00–24:00, every day), so Grafana does not even attempt delivery.
- Alerts still evaluate, keep state history, and show under Alerting > Alert rules.

To wire a real destination once one is supplied:

1. In `ttv-notifications.yaml`, replace the `ttv-unconfigured` receiver's `type` and `settings` with the real integration, for example an internal webhook or email addresses. Keep credentials out of the repository. To send stage breaches per team, add child routes under the `stage_breach` route that match `owner`.
2. Remove `mute_time_intervals: [ttv-until-destinations-supplied]` from the routes that should notify.
3. Update `test_notifications_are_inert_and_grouped` in `tests/test_alert_rules.py`. It fails on purpose once a destination is real, so the change is deliberate.
4. Restart Grafana and check Alerting > Notification policies. Do not send test messages to people only to demonstrate the integration.

### Blindness never becomes a stage accusation

- **The engine.** A stale source, an unmatched node or an identity conflict makes GPUs UNKNOWN. UNKNOWN GPUs have no stage owner, so they never count toward a stage breach.
- **The stage-breach rule.** It evaluates only while an ok or partial engine scan finished in the last 15 minutes and server-scan is within grace. Otherwise it returns no rows, and `KeepLast` holds each owner's instance: nothing new fires and nothing resolves.
- **Who hears about it.** The blindness reaches the platform operators as data-quality alerts, grouped separately from stage breaches.

Details and the known limit (one failing cluster) are in `docs/runbooks.md`, "Blindness never becomes a stage accusation".

### Alerts validation record (not production evidence)

2026-10-31, Grafana **12.4.14** (`docker.io/grafana/grafana@sha256:ae509d9ae8c1021ca32ec370a9ca975d40165eda9fccd1e1102d317ff8787cd7`), throwaway containers provisioned from these files:

- **Provisioning (`lab-tested`).** Both files provisioned with no errors or template-expansion errors. The rules sit in their own folder, and the dashboards folder `gpu-ttv` is unchanged. Alertmanager groups by `kind`, `owner`, `alertname` into `ttv-unconfigured`.
- **Lab ledger (`lab-tested`, read-only).** `ttv_lab2`, the lab ledger written by the running engine (sandbox server-scan plus cluster upi-tlv), was evaluated from 02:45 to 03:50 UTC. Every rule was healthy. Of the rules that remain, three fired exactly when their `for` elapsed (the run also covered two rules ADR-0008 removed since; their results are not repeated here):
  - **TTV UNKNOWN share high** at 03:16: 48.2% of GPUs UNKNOWN (2,299 of 4,768). By reason: CLUSTER_NOT_OBSERVED 1046, SOURCE_STALE 868 (records server-scan itself marks stale), IDENTITY_CONFLICT 266, NODE_NOT_FOUND 102, CONTESTED 16, TRAFFIC_UNOBSERVED 1; 229 servers have an unread GPU count.
  - **TTV node not found** (upi-tlv) at 03:16: 102 GPUs on 23 servers. The sandbox places servers in upi-tlv, and the lab cluster that stands in for it has no such nodes.
  - **TTV stage objective breached** for Redbull at 03:49: 214 GPUs NOT_IN_CLUSTER past the 2-day objective, ≥ 973 overdue GPU-hours, with stage clocks that are lower bounds from server-scan `created_at`.

  POPAI and Serving stayed Normal, with nothing overdue. Engine, sources and UNKNOWN growth stayed Normal. All queries ran in under 2 ms on 1,112 intervals.
- **No sending.** Over the whole lab run, the mute meant zero delivery attempts: Grafana handed alerts to its local Alertmanager 90 times, and no notify line or error was logged. An unmuted experiment showed what an attempt does: it failed locally with "connection refused" on `127.0.0.1:1`.
- **Simulation (`fixture-tested` in Grafana).** The real rule SQL ran against a synthetic ledger in a scratch database, with only `for`, `keepFiringFor` and group intervals shortened.
  1. Redbull's 8 GPUs, 3 h into a 1 h objective, fired; another owner within its objective stayed Normal.
  2. Server-scan failing past grace, with its GPUs UNKNOWN: the source alert fired (critical, `platform`), the UNKNOWN-share alerts fired, and both stage instances were held (`NoData, KeepLast`).
  3. With the engine stopped and that other owner's clock past its objective, "engine scans stale" fired and the owner stayed Normal (held).
  4. After a fresh scan, its breach fired.
  5. With `SELECT` on `capacity_intervals` revoked, the data-quality rules raised one `DatasourceError` group for the platform, and the stage instances were held (`Error, KeepLast`).

  Two bugs were found and fixed this way: detail queries that returned no rows (Grafana then marks the whole rule NoData), and `noDataState: Alerting`, which raised a false alert on a rule ADR-0008 removed since.
