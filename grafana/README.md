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
- **Size.** Validate at 1440×900 (laptop) and 2560×1440; no panel narrower than 4 grid units; no page with more than 8 KPI cards.

**Panel contracts.** Every non-row panel has a contract in `config/panel-contracts/<dashboard-uid>.yaml`; `tests/test_panel_contracts.py` checks the files against the dashboards (schema in `config/panel-contracts/README.md`).

## Reading the dashboards

- **Home** answers the main story in a minute: six cards (idle GPUs, their AWS-equivalent value per hour, productive share of eligible GPUs, unknown and not-measured GPUs, data age), the four stage queues in pipeline order with their oldest wait, every GPU by category, the ten longest waits, installed GPUs by model (E01, purchase dates "input missing"), and idle/overdue time and value in the selected period with a trend. Set `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/var/lib/grafana/dashboards/gpu-ttv/ttv-home.json` to open it first.
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
