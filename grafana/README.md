# Grafana: dashboards and data sources as code

The R1 pages of AGENT.md §20.11, read from the TTV ledger (`migrations/`): Home, Queues (E03), Delivery (E02), GPU use (E12), the asset drill-down and Evidence (E18), plus the later questions' pages in their missing-input state. Values are AWS On-Demand equivalents in USD (ADR-0003).

```
grafana/
  provisioning/datasources/ttv.yaml   Postgres uid ttv-postgres, Prometheus uid ttv-prometheus
  provisioning/dashboards/ttv.yaml    file provider -> folder "GPU Time-to-Value" (uid gpu-ttv)
  dashboards/ttv-home.json            uid ttv-home     "Home"
  dashboards/ttv-queues.json          uid ttv-queues   "Queues: where GPUs are stuck"
  dashboards/ttv-evidence.json        uid ttv-evidence "Evidence & monitoring health"
  dashboards/ttv-*.json               the other pages (table in "Design conventions")
```

Only built-in panels are used (stat, bar gauge, bar chart, table, time series, state timeline, text, row). There are no plugins, external fonts, CDNs or remote images, so the files work offline as they are.

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

## Design conventions (every page)

These rules keep the pages consistent (AGENT.md §13 "Page hierarchy and visual design", §20.11). A page that breaks one needs a reason in its panel contract.

**Pages and stable UIDs.** Every dashboard has the tag `gpu-ttv` and the same `links` bar: explicit `link` entries, in this order, each with `includeVars: true` and `keepTime: true`.

| Order | UID | Title | Questions | Release |
| --- | --- | --- | --- | --- |
| 1 | `ttv-home` | Home | E01 (partial), summary of E02, E03, E12, E18; E04 shown as missing input | R1 |
| 2 | `ttv-queues` | Queues: where GPUs are stuck | E03 | R1 |
| 3 | `ttv-delivery` | Delivery: are we getting faster? | E02; E07 as missing input | R1 |
| 4 | `ttv-gpu-use` | GPU use: are GPUs doing useful work? | E12 | R1 |
| 5 | `ttv-asset` | Asset: one server's GPUs and evidence | drill-down (`$asset_id`), links to `/explain/{asset}` | R1 |
| 6 | `ttv-evidence` | Evidence & monitoring health | E18 | R1 |
| 7 | `ttv-actions` | Team actions | E05, E16 (missing input) | R2/R3 |
| 8 | `ttv-capacity` | Capacity & commitments | E06, E08, E09, E14, E17 (missing input) | R3 |
| 9 | `ttv-service` | Service & adoption | E10, E11 (missing input) | R2 |
| 10 | `ttv-economics` | Economics & energy | E13, E15 (missing input) | R2/R3 |

The links bar titles are the short names (Home, Queues, Delivery, GPU use, Asset, Evidence, Actions, Capacity, Service, Economics).

**Semantic colours** (Grafana named colours, so both themes work). Use them only for these meanings, and never rely on colour alone (the category or reason is always written too):

| Meaning | Colour |
| --- | --- |
| Productive / healthy / within policy | `green` |
| Idle / needs attention | `orange` |
| Overdue / problem / failing | `red` |
| NOT_CHARGED / approved exclusion | `blue` |
| UNKNOWN / not measured / unpriced | `purple` |
| Neutral counts (inventory, totals) | `text` |

**Ordering.** Stage owners always appear in pipeline order DC, Redbull, POPAI, Serving: `ORDER BY array_position(ARRAY['DC','Redbull','POPAI','Serving'], stage_owner)`. Categories: PRODUCTIVE, IDLE, NOT_CHARGED, UNKNOWN.

**Variables.** Reuse Queues' definitions verbatim where a page needs them: `stage_owner` and `site` (multi, All = `'__all__'`, filter as `('__all__' IN ($site) OR coalesce(a.site, '(none)') IN ($site))`) and `snapshot`. Each panel's description lists the filters it honours.

**Panel rules.**
- **Stat panels show one number each**, `graphMode: none` unless the panel is a trend, `textMode: value_and_name` only for deliberate groups. Set `text.titleSize` and `text.valueSize` explicitly so cards in one row match (KPI cards: value 40, title 14). Never let a string field become the stat value by accident: select the numeric field with `reduceOptions.fields`, or use a table.
- **Words are not stats.** Identifiers, timestamps, versions and policy names go in a table, a small "as-of" table, or the panel description, never in a stat that shrinks them to unreadable size.
- **Tables fit their rows.** Size the panel so the expected rows show without an inner scrollbar (about 1 grid unit per row plus 2), give identifier columns a width, right-align numbers, and use `color-text` (never full `color-background`) for status cells. No raw JSON columns: pick the fields that matter and name them in plain words.
- **Asset cells link to the drill-down:** `/d/ttv-asset?var-asset_id=${__value.raw}&${__url_time_range}`.
- **Trends are line charts** (`timeseries`, lines, fill ≤ 10, no points), comparisons are bars, aging is a distribution or a sorted table, actions are tables. Gauges only with a meaningful target.
- **Over-time series from the ledger** sample open intervals on a grid, never by summing interval rows:
  `FROM generate_series($__timeFrom()::timestamptz, $__timeTo()::timestamptz, make_interval(secs => greatest($__interval_ms, 60000) / 1000.0)) AS t JOIN capacity_intervals i ON i.started_at <= t AND (i.ended_at IS NULL OR i.ended_at > t)`.
- **Lower bounds** display as `≥ X` (text in tables; a `≥` prefix override on stats when the query says the value is a lower bound).
- **Unknown is never zero or green.** Panels with no evidence say so in `noValue` ("no evidence yet", "insufficient history: n < 5"), never `0`.
- **Missing-input panels** (unbuilt questions) are `text` panels titled `E0X · <question>` with: what is missing, where it would come from, the onboarding step, and the release. They never show a number.
- **Rows.** Group a page into at most 4 collapsible rows: "Now" (current state) first, then "Selected period" panels. Current-state and period panels are never mixed in one row.
- **Times.** Store and query UTC; tables use Grafana time fields (`unit: dateTimeAsIso` or `dateTimeFromNow`) so the dashboard timezone setting applies; never `to_char` a timestamp unless it needs a `≥` prefix.
- **Size.** Validate at 1440×900 (laptop) and 2560×1440; no panel narrower than 4 grid units; at most 8 KPI cards per row group, except where §20.11 lists more for Home.

**Panel contracts.** Every non-row panel has a contract in `config/panel-contracts/<dashboard-uid>.yaml`; `tests/test_panel_contracts.py` checks the files against the dashboards (schema in `config/panel-contracts/README.md`).

**Missing-input pages, question map and query tests.** `dashboards/ttv-actions.json`, `ttv-capacity.json`, `ttv-service.json` and `ttv-economics.json` hold one text card per R2/R3 question: the missing inputs, where they would come from, the onboarding step, the release and the R1 page that answers the nearest question today. They have no variables and run no queries. `docs/executive-question-map.md` maps E01–E18 to pages and panels. `tests/test_dashboards_sql.py` runs every panel and variable query of every page against a migrated, seeded Postgres (`TTV_TEST_DSN`); `tests/grafana_sql.py` substitutes macros and variables the way Grafana's Postgres datasource does.

## Reading the dashboards

- **Home** answers the main story in a minute: six cards (idle GPUs, their AWS-equivalent value per hour, productive share of eligible GPUs, unknown and not-measured GPUs, data age), the four stage queues in pipeline order with their oldest wait, every GPU by category, the ten longest waits, installed GPUs by model (E01, purchase dates "input missing"), idle/overdue time and value in the selected period with a trend, and Delivery's p50/p90 time to GPU-ready and to first genuine request (same queries, last 28 complete days). Set `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/gpu-ttv/ttv-home.json` to open it first.
- **Value cards** show a number with a line above it: the snapshot's region and price date, and "at least (N GPUs unpriced)" when some GPUs have no rate, so the number is a lower bound.

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
- **Delivery (E02).** Clocks stay separate: first record → GPU-ready → workload running → first genuine request, end-to-end per server from its own milestones, and one clock per stage episode (DC, Redbull, POPAI, Serving). The comparison is the last `$period` complete UTC days (default 28) against the period before. Percentiles use exact samples, whose ends the engine saw happen. With fewer than 5, they use exact plus lower-bound samples and show `≥`; with fewer still, "insufficient history (n = …)". Samples whose end was already true at first sight, rework and episodes that ended UNKNOWN are left out and counted as "excl.". "Quicker" is green only when nothing in the backlog is overdue. Cohort cells read "not matured" or `≥` rather than guess. Rules: `docs/adr/0005-delivery-clocks.md`. E07 (backlog flow) is Release 2.
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
4. Milestones, `scan_runs.started_at` and interval boundaries written by one scan carry the same timestamp (Delivery dates a transition exactly only when an interval ended at that scan).

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

## Alerts

R1 alerts for monitoring blindness and stage-objective breaches (AGENT.md §20, §14). They are Grafana-managed rules that read the ledger, with no Prometheus dependency. The design is in ADR-0006; the runbooks and the rule contract table are in `docs/runbooks.md`.

```
grafana/provisioning/alerting/
  ttv-rules.yaml           8 rules in folder "GPU Time-to-Value alerts":
                           group ttv-data-quality (1m), group ttv-stage-objectives (5m)
  ttv-notifications.yaml   inert contact point, always-on mute timing, policy tree
                           grouped by kind, owner, alertname
```

| Rule | kind | owner | Fires when |
| --- | --- | --- | --- |
| TTV engine scans stale | data_quality | platform | No ok/partial engine scan for > 15 min (for 5m) |
| TTV source failing past grace | data_quality | platform | A server-scan, kube or Thanos source failed ≥ 2 consecutive scans (for 5m) |
| TTV collector manager stale | data_quality | platform | A manager has no complete collector read for > 60 min, or no collector read exists (for 15m) |
| TTV UNKNOWN share high | data_quality | platform | > 10% of ledger GPUs UNKNOWN (for 30m) |
| TTV UNKNOWN share growing | data_quality | platform | UNKNOWN share up > 5 points in 1 h (for 10m) |
| TTV handover overdue | data_quality | platform | OCP-profile servers missing from server-scan past `handover_grace`, per manager (for 15m) |
| TTV node not found | data_quality | platform | `NODE_NOT_FOUND` GPUs in an observed cluster (for 30m) |
| TTV stage objective breached | stage_breach | the stage owner | Idle GPUs past their stage objective, one instance per owner (for 1h, keep firing 1h) |

### Install

- **Mount** `provisioning/alerting/` at `/etc/grafana/provisioning/alerting/`, next to the datasource and dashboard mounts above.
  - A malformed alerting file stops Grafana from starting. `uv run pytest tests/test_alert_rules.py` checks both files.
  - Alerting provisions before dashboards, which is why the rules have their own folder: a shared title would create a second "GPU Time-to-Value" folder.
- **Grants.** The Grafana database user also needs `SELECT` on `collector_reads` (`migrations/0003_collector.sql`). The rules read `scan_runs`, `schema_migrations`, `source_state`, `capacity_intervals`, `stage_objectives`, `assets`, `next_action_annotations` and `collector_reads`. A missing grant shows as a `DatasourceError` or `DatasourceNoData` alert for the platform, never as a healthy rule.
- **Links.** `dashboard_url` and `asset_url` are built from Grafana's `root_url` (`GF_SERVER_ROOT_URL`); set it so links are absolute. `asset_url` opens the Asset page (`ttv-asset`) for the longest-waiting server.
- **Runbooks.** `runbook_url` points at `https://github.com/roiblum1/gpu-ttv/blob/main/docs/runbooks.md#<rule uid>`. For the air-gapped install, rewrite that prefix to the internal Git mirror in `ttv-rules.yaml`, and change `RUNBOOK_BASE` in `tests/test_alert_rules.py` to match.
- **Shared Grafana.** `ttv-notifications.yaml` replaces the organisation's whole notification policy tree. In a Grafana that other teams use, mount only `ttv-rules.yaml` and add the two routes to the existing tree by hand.
- **Thresholds.** Thresholds copy engine settings: 300 s scans, `TTV_UNKNOWN_GRACE_SCANS=2`, `TTV_COLLECTOR_STALE_AFTER=1h`, `collector.handover_grace=6h`. Edit the rule when one of them changes. The UNKNOWN-share thresholds are provisional.

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

- **The engine.** A stale source or manager, an unmatched node or a pending handover makes GPUs UNKNOWN. UNKNOWN GPUs have no stage owner, so they never count toward a stage breach.
- **The stage-breach rule.** It evaluates only while an ok or partial engine scan finished in the last 15 minutes and server-scan is within grace. Otherwise it returns no rows, and `KeepLast` holds each owner's instance: nothing new fires and nothing resolves.
- **Who hears about it.** The blindness reaches the platform operators as data-quality alerts, grouped separately from stage breaches.

Details and the known limit (one failing cluster) are in `docs/runbooks.md`, "Blindness never becomes a stage accusation".

### Alerts validation record (not production evidence)

2026-10-31, Grafana **12.4.14** (`docker.io/grafana/grafana@sha256:ae509d9ae8c1021ca32ec370a9ca975d40165eda9fccd1e1102d317ff8787cd7`), throwaway containers provisioned from these files:

- **Provisioning (`lab-tested`).** Both files provisioned with no errors or template-expansion errors. The rules sit in their own folder, and the dashboards folder `gpu-ttv` is unchanged. Alertmanager groups by `kind`, `owner`, `alertname` into `ttv-unconfigured`.
- **Lab ledger (`lab-tested`, read-only).** `ttv_lab2`, the lab ledger written by the running engine (sandbox server-scan plus cluster upi-tlv), was evaluated from 02:45 to 03:50 UTC. Every rule was healthy, and four fired exactly when their `for` elapsed:
  - **TTV UNKNOWN share high** at 03:16: 48.2% of GPUs UNKNOWN (2,299 of 4,768). By reason: CLUSTER_NOT_OBSERVED 1046, SOURCE_STALE 868 (records server-scan itself marks stale), IDENTITY_CONFLICT 266, NODE_NOT_FOUND 102, CONTESTED 16, TRAFFIC_UNOBSERVED 1; 229 servers have an unread GPU count.
  - **TTV node not found** (upi-tlv) at 03:16: 102 GPUs on 23 servers. The sandbox places servers in upi-tlv, and the lab cluster that stands in for it has no such nodes.
  - **TTV collector manager stale**, as `(no collector reads)`, at 03:29: no collector runs in the lab, so DC's stage is unobserved.
  - **TTV stage objective breached** for Redbull at 03:49: 214 GPUs NOT_IN_CLUSTER past the 2-day objective, ≥ 973 overdue GPU-hours, with stage clocks that are lower bounds from server-scan `created_at`.

  DC, POPAI and Serving stayed Normal, with nothing overdue. Engine, sources and UNKNOWN growth stayed Normal. Handover overdue held state (`NoData, KeepLast`), because there are no collector managers. All queries ran in under 2 ms on 1,112 intervals.
- **No sending.** Over the whole lab run, the mute meant zero delivery attempts: Grafana handed alerts to its local Alertmanager 90 times, and no notify line or error was logged. An unmuted experiment showed what an attempt does: it failed locally with "connection refused" on `127.0.0.1:1`.
- **Simulation (`fixture-tested` in Grafana).** The real rule SQL ran against a synthetic ledger in a scratch database, with only `for`, `keepFiringFor` and group intervals shortened.
  1. Redbull's 8 GPUs, 3 h into a 1 h objective, fired; DC within its objective stayed Normal.
  2. Server-scan failing past grace, with its GPUs UNKNOWN: the source alert fired (critical, `platform`), the UNKNOWN-share alerts fired, and both stage instances were held (`NoData, KeepLast`).
  3. With the engine stopped and DC's clock past its objective, "engine scans stale" fired and DC stayed Normal (held).
  4. After a fresh scan, DC's breach fired.
  5. With `SELECT` on `capacity_intervals` revoked, the data-quality rules raised one `DatasourceError` group for the platform, and the stage instances were held (`Error, KeepLast`).

  Two bugs were found and fixed this way: detail queries that returned no rows (Grafana then marks the whole rule NoData), and `noDataState: Alerting`, which raised a false "collector manager stale".
