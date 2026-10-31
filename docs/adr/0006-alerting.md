# ADR-0006: R1 alerts as Grafana-managed rules on the ledger

- **Status:** accepted 2026-10-31.
  - `fixture-tested`: every rule query against synthetic ledgers (`tests/test_alert_rules_pg.py`).
  - `lab-tested`: provisioned in Grafana 12.4.14 and evaluated against the lab ledger (`grafana/README.md`, "Alerts").
  - Not production evidence. No notification has been sent anywhere.
- **Context:** AGENT.md §20 (R1: "alerts for monitoring blindness and stage-objective breaches"; ADR-0002 rows 5 and 12 raise alerts) and §14 (content, grouping, hysteresis, grace; a missing collector raises a data-quality issue, never an accusation; destinations unconfigured until supplied).
- **Files:**
  - Rules and routing: `grafana/provisioning/alerting/ttv-rules.yaml`, `grafana/provisioning/alerting/ttv-notifications.yaml`.
  - Runbooks and the rule contract table: `docs/runbooks.md`.
  - Tests: `tests/test_alert_rules_pg.py`, `tests/test_alert_rules.py`.

## Decision

**Eight Grafana-managed rules read the Postgres ledger.** The ledger is the source of truth (§9), so an alert and a dashboard panel cannot disagree. The engine's Prometheus gauges add nothing the ledger lacks, and no Prometheus is reachable in the lab, so no rule depends on them. An engine-liveness rule on `ttv_scan_last_success_timestamp_seconds` can be added later. It would also catch a ledger that is unreachable from the engine but not from Grafana.

| Group (interval) | Rule | kind / owner |
| --- | --- | --- |
| `ttv-data-quality` (1m) | engine scans stale, source failing past grace, collector manager stale, UNKNOWN share high, UNKNOWN share growing, handover overdue (row 5), node not found (row 12) | `data_quality` / `platform` |
| `ttv-stage-objectives` (5m) | stage objective breached, one instance per stage owner | `stage_breach` / the stage owner |

Thresholds, `for` and `keepFiringFor` are in `docs/runbooks.md`, "Rule contracts". They assume the engine defaults: 5-minute scans, grace 2, collector stale after 1 h, handover grace 6 h. The UNKNOWN-share thresholds (10%, +5 points in 1 h) are provisional defaults, not approved policy.

### Shape of every query

Grafana turns a SQL table into alert instances only when it has **exactly one numeric column**; the text columns become labels. So:

- Query A gives the value with **stable** labels: the entity (`source`, `manager`, `cluster`, `stage_owner`) or none.
- Query B gives the annotation detail as **volatile** text labels, such as the oldest assets, the last error and the as-of time. Annotations read it as `$values.B.Labels.x` and `$values.B.Value`.
  - Grafana matches B to an instance when B's labels contain the instance's labels.
  - Detail never sits in A's labels, because a changing label would create a new instance every scan and reset `for`.
- **Healthy entities return 0, not nothing.** No rows (NoData) therefore always means missing evidence, never "healthy".
- **B covers every entity of A.** Grafana marks the *whole rule* NoData when any query returns no rows, or when a query other than the condition's fails. A detail query that returned rows only for problem entities would therefore have pushed healthy rules into NoData; a lab simulation caught this. `tests/test_alert_rules_pg.py` asserts that B has a row for every instance of A.

### Missing evidence is never healthy

No rule uses `OK` for NoData or errors.

- **Rules that always return a row while the ledger is readable** (engine scans, collector managers, UNKNOWN share): NoData → `NoData`, error → `Error`. Grafana raises `DatasourceNoData` or `DatasourceError` with the rule's labels (`data_quality`/`platform`) and its title in `rulename`.
  - `Alerting` was tried first. When only the detail query failed, "collector manager stale" fired for a manager that was not stale. `NoData` reports "this rule cannot be evaluated" without a false claim.
  - "Never" is a value, not a gap. With no engine scan, age counts from the schema's creation; with no collector read, a `(no collector reads)` row fires, because DC's stage is then unobserved.
- **Per-entity data-quality rules:** NoData → `KeepLast`, error → `Error`. Their NoData only happens when the rule is gated, or when the engine has not attempted any source for an hour, and "engine scans stale" reports both.
- **Stage objective breached:** NoData and error → `KeepLast`. See below.

### Blindness never becomes a stage accusation

1. **The engine suppresses first.** Failing sources (past grace), stale collector managers, unmatched nodes and pending handovers make GPUs UNKNOWN. UNKNOWN GPUs have no stage owner, so they never enter a stage-breach count.
2. **A Grafana gate covers what the engine cannot.** A dead engine leaves intervals IDLE while the overdue clock in SQL keeps running, and a server-scan outage removes most GPUs from every queue at once. The stage-breach query returns rows only while an ok or partial engine scan finished in the last 15 minutes and server-scan is within grace. Otherwise it returns nothing, and `KeepLast` freezes every owner's instance: nothing is raised or resolved, and the annotations keep their last as-of time. Validated in Grafana 12.4.14: under `KeepLast`, NoData and query errors keep each instance's state and annotations.
3. **`execErrState: KeepLast` for stage breaches** means a query error there raises nothing by itself. Two things cover it:
   - every data-quality rule reads the same database and raises `Error` (a `DatasourceError` alert, `owner=platform`);
   - the Postgres test runs every rule query against the current migrations, so a schema change that breaks a query fails the gate.
4. **Owner labels fall back.** The stage-breach rule's `kind` and `owner` labels are templated from `stage_owner`. Any instance without one (error or no data before the first evaluation) is labelled `data_quality` / `platform`.

Known limit: when a single cluster or manager fails, its GPUs turn UNKNOWN. An owner whose overdue GPUs were all there sees the breach resolve after `keepFiringFor` (1 h) while "UNKNOWN share growing" and "source failing" fire. Freezing all stage alerts whenever any source fails would silence real breaches elsewhere for the whole outage, so this is accepted.

No inhibition rules are used. The gate and the engine's UNKNOWN classification already stop the symptom alerts, and grouping by `kind` keeps one source outage to one platform group.

### Provisioning facts found while validating (Grafana 12.4.14)

- **Labels are environment-expanded; annotations are not.** `{{ $labels.x }}` in a provisioned *label* became `{{ .x }}` (`$labels` read as an unset variable). Label templates therefore use `.Labels.x`, and a literal `$` is written `$$`. `tests/test_alert_rules.py` forbids `$` in labels.
- **Alerting provisions before dashboards,** and group-level `folderUid` is ignored. A rule folder titled "GPU Time-to-Value" created a second folder with the dashboards' title, so the rules use their own folder, "GPU Time-to-Value alerts".
- **A broken alerting file stops Grafana from starting** (for example, a policy naming a missing contact point), so the files are checked statically in tests.
- **A contact point with no integrations is not created.** The policy tree needs a receiver, so `ttv-unconfigured` is a webhook to a closed loopback port (`127.0.0.1:1`).
  - Without the mute, Grafana's attempt failed locally with "connection refused".
  - The always-on mute timing `ttv-until-destinations-supplied` on both routes means no attempt is made at all.
- `keepFiringFor` (camelCase) is honoured in file provisioning. `missingSeriesEvalsToResolve` was not, so the default of 2 evaluations applies.
- `execErrState: Error` replaces a rule's existing instances with one rule-level instance that carries only the rule's labels. That is why the stage-breach labels need their `data_quality`/`platform` fallback, and why that rule uses `KeepLast` instead.
- On a fresh Grafana database, the provisioned notification policy reaches the Alertmanager about a minute after start; until then Grafana's default policy applies. Every TTV rule has `for` of 5 minutes or more, so no TTV alert can fire in that window.

### Notification routing

`ttv-notifications.yaml` replaces the organisation's notification policy tree. The root groups by `kind`, `owner` and `alertname`, and there is one muted route per kind:

- data quality: group wait 1 m, repeat 4 h;
- stage breach: group wait 5 m, repeat 24 h (a daily reminder per owner).

In a shared Grafana, do not mount this file: merge the two routes into the existing tree. Wiring a destination is in `grafana/README.md`, "Alerts".

## Consequences

- Alert thresholds duplicate engine settings (`TTV_UNKNOWN_GRACE_SCANS`, `TTV_COLLECTOR_STALE_AFTER`, the scan interval, `collector.handover_grace`). Changing one means editing the rule; `.env.example` says so.
- The engine must keep the evidence keys the rules read:
  - `record_stale` and `source_failed_scans` on `SOURCE_STALE`;
  - `manager`, `name`, `profile_name` and `ocp_profile_since` on collector buckets;
  - `cluster_name` on `CLUSTER_UNREACHABLE`;
  - `cluster` and `node_name` on `NODE_NOT_FOUND`;
  - the `handover_overdue` flag.

  The Postgres end-to-end test covers the server-scan path; the others are covered by synthetic rows shaped like the engine's.
- `asset_url` annotations open the Asset page (`ttv-asset`) for the first asset the detail query names.
