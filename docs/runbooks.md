# Alert runbooks (Release 1)

The R1 alerts cover monitoring blindness and stage-objective breaches (AGENT.md §20, §14). They are Grafana-managed rules provisioned from `grafana/provisioning/alerting/ttv-rules.yaml`, and they read the ledger in Postgres. The design and its trade-offs are in ADR-0006; installation and wiring are in `grafana/README.md`, "Alerts".

Every alert carries:

- **Labels:** `severity` (`critical` or `warning`), `kind` (`data_quality` or `stage_breach`) and `owner`. `owner` is `platform` for every data-quality alert and the stage owner (DC, Redbull, POPAI, Serving) for a stage breach.
- **Annotations:** `summary`, `description` (counts, affected assets, evidence, duration), `impact`, `confidence`, `next_step`, `runbook_url` (this file), `dashboard_url`, and `asset_url` where one asset is the place to start. Each rule also links one panel (`__dashboardUid__`/`__panelId__`).

**No notification destination is configured.** The contact point `ttv-unconfigured` is inert and both routes are muted (`grafana/README.md`, "Alerts"). Until destinations are supplied, read alerts in Grafana under Alerting > Alert rules, folder "GPU Time-to-Value alerts".

## Who acts

| Kind | Owner label | Who acts |
| --- | --- | --- |
| `data_quality` | `platform` | The platform operators who run ttv-engine, ttv-collector, Postgres and Grafana. They may hand a finding to Redbull (server-scan data) or a cluster's admins, but the alert never accuses a stage team. |
| `stage_breach` | the stage owner | That team clears the wait or records a next-action owner (display only: it never moves the breach or the GPU-hours). |

## Blindness never becomes a stage accusation

1. **In the engine.** A source failing past `TTV_UNKNOWN_GRACE_SCANS` (2) turns its assets UNKNOWN (`SOURCE_STALE`, `CLUSTER_UNREACHABLE`), and a stale collector manager turns its assets `SOURCE_STALE`. UNKNOWN GPUs have no stage owner, so they leave every stage queue and cannot breach an objective. `NODE_NOT_FOUND` and `HANDOVER_PENDING` are UNKNOWN for the same reason. This is the main suppression mechanism (ADR-0002 rows 2, 5, 11, 12).
2. **In Grafana.** Two gaps remain, and the stage-breach rule closes them:
   - A dead engine writes nothing. Open intervals stay IDLE while the SQL's overdue clock keeps running.
   - A server-scan outage turns most GPUs UNKNOWN at once. Every owner's count would drop, and their breaches would resolve without evidence.

   So the rule evaluates only while the newest ok or partial engine scan is under 15 minutes old and server-scan is within its grace. Otherwise it returns no rows. With `noDataState: KeepLast` and `execErrState: KeepLast`, every stage-breach instance then keeps its last state and annotations: nothing new is raised and nothing is resolved. The data-quality rules report the blindness.
3. **In routing.** Data-quality and stage-breach alerts are separate groups (`kind`, `owner`, `alertname`), so a source outage reaches the platform operators as one group, not as one message per stage team.

Known limit: when only one cluster or one manager fails, its GPUs turn UNKNOWN. An owner whose overdue GPUs were all there sees the breach resolve after `keepFiringFor` (1 h). "TTV UNKNOWN share growing" and "TTV source failing past grace" fire for the same event and explain it.

## States when evidence is missing

Grafana treats a rule as NoData when **any** of its queries returns no rows, or when a query other than the condition's fails (lab-tested on 12.4.14). So every detail query (B) returns a row for every entity that query A returns.

| Rule shape | No rows (NoData) | Query error | Why |
| --- | --- | --- | --- |
| Always one row while the ledger is readable (engine scans, collector managers, UNKNOWN share) | `NoData` | `Error` | No data here means the ledger holds nothing to measure, or a detail query failed. Grafana raises `DatasourceNoData` to the platform; it is never "healthy", and the rule makes no claim it cannot support. |
| One row per known entity, 0 when healthy (sources, handover, node not found, UNKNOWN growth) | `KeepLast` | `Error` | Healthy entities return 0, so no rows only happens when the rule is gated or has no history. Another rule reports that. |
| Stage objective breached | `KeepLast` | `KeepLast` | Blindness must neither accuse nor clear a team. A failing query is caught by the data-quality rules on the same database, and `tests/test_alert_rules_pg.py` runs every query against the current migrations. |

`Error` raises Grafana's `DatasourceError` alert and `NoData` raises `DatasourceNoData`, each with the rule's labels and its title in `rulename`. Both route as `kind=data_quality`, `owner=platform`. For the stage-breach rule those labels fall back to `data_quality`/`platform` whenever there is no `stage_owner`. No rule uses `OK` for missing evidence.

## Runbooks

### TTV engine scans stale

- **Means:** no engine scan with status `ok` or `partial` has finished for more than 15 minutes (three missed 5-minute scans). With no engine scan at all, the age counts from when the ledger schema was created. Fires after 5 more minutes; severity `critical`.
- **Evidence:** Evidence (`/d/ttv-evidence`), "Data age" (panel 40), "Engine scans per hour, by result" (panel 17) and "Recent partial or failed scans" (panel 19). The description gives the newest scan's status and errors.
- **Likely causes:** the ttv-engine pod is down or crash-looping; it lost Postgres; another replica holds the advisory lock but is stuck; every scan ends `failed` (server-scan unreachable from a cold start); the engine runs with `TTV_DRY_RUN=true` (status `dry_run` never updates the ledger, so this alert fires by design during the §20.6 dry run).
- **Next step:** check the pod's logs (`scan.done`, `serverscan.fetch_failed`), `/readyz`, and `SELECT status, started_at, errors FROM scan_runs WHERE kind = 'engine' ORDER BY id DESC LIMIT 5`.
- **Who acts:** platform operators.
- **While it fires:** stage-breach alerts hold their last state; every "now" panel is as old as "Data age".

### TTV source failing past grace

- **Means:** a source in `source_state` (`serverscan`, `kube:<cluster>`, `thanos:<cluster>`) has failed 2 or more consecutive engine scans, so its assets are UNKNOWN. One instance per source attempted in the last hour. Severity `critical` for `serverscan`, else `warning`. Fires after 5 minutes and keeps firing 10 minutes after recovery.
- **Evidence:** Evidence, "Source health" (panel 21). The description gives the last success, the last error and how many GPUs are UNKNOWN because of this source.
- **Likely causes:** server-scan API down, its token expired or revoked, or a CA bundle change; a cluster API or Thanos querier unreachable, its service-account token rotated, or a read slower than `TTV_CLUSTER_TIMEOUT_SECONDS`.
- **Next step:** from the engine pod, call the source read-only: one page of server-scan's `GET /api/v1/servers` with the auditor token and the CA bundle; `oc --kubeconfig … get nodes`; a Thanos `query?query=up`. Never call server-scan's `/servers/available`: despite being a GET, it writes.
- **Who acts:** platform operators; the cluster's admins for kube or Thanos access.
- **Note:** a source removed from configuration stops being attempted and drops out after an hour. The threshold assumes `TTV_UNKNOWN_GRACE_SCANS=2`; change both together.

### TTV collector manager stale

- **Means:** a manager in `TTV_MANAGERS_FILE` (or one read before and not since removed) has had no complete read for more than 60 minutes (`TTV_COLLECTOR_STALE_AFTER`), counted from when it was configured if it was never read. The engine has stopped trusting it, so its collector-only servers are `SOURCE_STALE`. The special manager `(no collector reads)` means the collector has never stored a read: GPU servers without an OCP profile (DC's stage) are not observed at all. Fires after 15 minutes; severity `warning`.
- **Evidence:** Evidence, "Profile-less collector: managers" (panel 30). The description gives the last complete read, the last attempt with its errors, and the GPUs now UNKNOWN.
- **Likely causes:** the ttv-collector job is not deployed or not scheduled; manager credentials or CA changed; the manager is unreachable or slower than `TTV_COLLECTOR_TIMEOUT_SECONDS`; the manager was decommissioned but is still in `TTV_MANAGERS_FILE`.
- **Next step:** run `ttv-collector --once --dry-run --servers` against that manager and read the job's logs. For `(no collector reads)`, check that the job runs and that `TTV_MANAGERS_FILE` lists the managers. For a decommissioned manager, remove it from `TTV_MANAGERS_FILE` and restart the job: it is marked removed, the alert resolves, and its servers retire after the tombstone period unless another source lists them (ADR-0004, "Removing a manager").
- **Who acts:** platform operators.

### TTV UNKNOWN share high

- **Means:** more than 10% of the GPUs in open ledger intervals (every category) are UNKNOWN. Fires after 30 minutes. With no open interval at all the share cannot be computed, and Grafana raises `DatasourceNoData` for this rule.
- **Evidence:** Evidence, "Unknown GPUs now, by reason" (panel 25) and "Known state" (panel 41). The description lists GPUs by reason and the servers whose GPU count is unread (never zero, not in the ledger).
- **Likely causes by reason:** `SOURCE_STALE` (source failure, or server-scan marks records stale); `CLUSTER_NOT_OBSERVED` (a target cluster missing from `TTV_CLUSTERS_FILE`); `CLUSTER_UNREACHABLE`; `IDENTITY_CONFLICT` (see "Open identity conflicts"); `NODE_NOT_FOUND`; `HANDOVER_PENDING`; `CONTESTED`; `TRAFFIC_UNOBSERVED` (no counter of record for a serving deployment).
- **Next step:** fix the largest reason first; each has its own alert or Evidence panel.
- **Who acts:** platform operators.
- **Threshold:** 10% is a provisional default, not an approved policy. Tune it in the rule.

### TTV UNKNOWN share growing

- **Means:** the UNKNOWN share rose by more than 5 percentage points within the last hour. The ledger is sampled now and an hour ago. Fires after 10 minutes; with less than an hour of history the rule has no value and holds its state.
- **Evidence:** Evidence, panels 25 and 31. The description lists the change by reason (for example `CLUSTER_UNREACHABLE +480`).
- **Likely causes:** a source just crossed its grace (a cluster or server-scan outage); a policy or clusters-file change; a burst of identity conflicts after a server-scan import.
- **Next step:** follow the reason that grew: "TTV source failing past grace", "TTV collector manager stale", "TTV node not found" or Evidence, "Open identity conflicts". Expect stage-breach counts to drop for monitoring reasons while this fires; that is not an improvement.
- **Who acts:** platform operators.

### TTV handover overdue

- **Means:** the profile-less collector saw an OCP profile bound to a GPU server more than `collector.handover_grace` (6 h) ago, but server-scan still does not list it. The server is `HANDOVER_PENDING` (UNKNOWN), charged to nobody, and flagged `handover_overdue` (ADR-0002 row 5). One instance per collector manager. Evaluated only while server-scan's latest read succeeded; otherwise it holds its state. Fires after 15 minutes.
- **Evidence:** Evidence, panel 25 (`HANDOVER_PENDING`). The description lists the oldest servers with their profile and the time the profile was first seen; `asset_url` opens the oldest one.
- **Likely causes:** server-scan's collector for that manager filters the server out (its name pattern: `collector_name_pattern`, `ucs_central_name_pattern`, `ome_name_pattern`, …); server-scan's collector for that manager is failing or has not run since the profile was bound; a serial normalisation mismatch, which makes the same server look like two.
- **Next step:** for the oldest server, compare its profile name with server-scan's pattern for that manager, and check server-scan's last run for that collector (read-only GETs). If server-scan does list it under another serial, raise an identity conflict.
- **Who acts:** platform operators; Redbull if server-scan's configuration must change.

### TTV node not found

- **Means:** server-scan places a GPU server `INSTALLED` in an observed target cluster, but no node of that cluster has its name. The name is `reported_name`, else the server-scan name, cleaned as server-scan cleans it (ADR-0002 row 12, D10). The GPUs are `NODE_NOT_FOUND` (UNKNOWN). One instance per cluster. Fires after 30 minutes and keeps firing 15 minutes after recovery.
- **Evidence:** Evidence, panel 25 (`NODE_NOT_FOUND`). The description lists the oldest servers with the node name expected; `asset_url` opens the oldest one.
- **Likely causes:** the node was removed or renamed but server-scan's membership was not updated; node names carry a domain suffix or case that the cleaning rule does not strip; the server is still joining and the node object does not exist yet; server-scan's `reported_name` is wrong.
- **Next step:** compare the expected name with `oc --kubeconfig <cluster> get nodes -o name` (read-only). If many servers of one cluster appear at once, suspect the naming rule rather than missing nodes.
- **Who acts:** platform operators; Redbull to correct server-scan membership.

### TTV stage objective breached

- **Means:** GPUs in open IDLE intervals have been in their current stage episode longer than the stage objective of their policy version (`stage_objectives`). One instance per stage owner, with severity `warning`. Fires after 1 hour above zero and keeps firing for 1 hour after it clears. The objectives are provisional (AGENT.md I1: DC 3 d, Redbull 2 d, POPAI 1 d, Serving 7 d).
- **Evidence:** Queues filtered to the owner (`/d/ttv-queues?var-stage_owner=<owner>`), "Longest waits" (panel 14) and "Idle and overdue GPU-hours" (panel 12). The description gives:
  - the overdue GPUs and the overdue GPU-hours accrued so far in their current stage episodes (`≥` when a stage clock began before observation);
  - the five longest waits, with reasons, days in stage, the objective and any next-action owner;
  - the idle GPUs that cannot be judged (no objective for their policy version, or no stage clock);
  - the ledger's as-of time.
- **Likely causes:** by reason. `NOT_IN_CLUSTER`/`IN_MCE_INVENTORY`/`NODE_NOT_READY` (Redbull provisioning); `GPU_PLATFORM_NOT_READY`/`GPU_MISSING` (POPAI); `NO_WORKLOAD`/`NO_REAL_DEMAND`/`WORKLOAD_NOT_READY`/`OTHER_WORKLOAD` (Serving); `PROFILE_NONE`/`MAINTENANCE`/`HARDWARE_CRITICAL`/`BMC_UNREACHABLE` (DC).
- **Next step:** open the first asset (`asset_url`) and the engine's `/explain/{asset}`, confirm the evidence, then ask the stage owner to clear it. If another team blocks it, record a next-action owner with a reason and an expiry. The breach stays with the stage owner.
- **Who acts:** the stage owner named in `owner`.
- **Not counted:** UNKNOWN and NOT_CHARGED GPUs. While the engine is stale or server-scan is failing, the alert holds its last state; see "Blindness never becomes a stage accusation" above.

## Rule contracts

The rules live in `grafana/provisioning/alerting/ttv-rules.yaml`, keyed by uid. Query A feeds the threshold, and query B fills the annotations.

- Every SQL returns one numeric column, plus text columns that become labels.
- Thresholds assume the engine defaults: 5-minute scans, `TTV_UNKNOWN_GRACE_SCANS=2`, `TTV_COLLECTOR_STALE_AFTER=1h` and `collector.handover_grace=6h`. Change a rule when you change these.
- Tests:
  - `tests/test_alert_rules_pg.py` (Postgres, `TTV_TEST_DSN`) runs each query on synthetic ledger rows and applies the rule's own threshold.
  - `tests/test_alert_rules.py` checks the files' structure, labels, links and routing.

| Rule (uid) | Query A: value, one instance per | Threshold | `for` / keep firing | No data / error | Tests |
| --- | --- | --- | --- | --- | --- |
| `ttv-engine-scans-stale` | Minutes since the newest `ok`/`partial` engine scan finished (else since the schema was created); one instance | > 15 | 5m / 0 | NoData / Error | `test_engine_scans_stale_fires_only_without_a_recent_ok_or_partial_scan`, `test_engine_never_scanned_counts_from_ledger_creation_not_zero` |
| `ttv-source-failing-past-grace` | `source_state.failed_scans`; per source attempted in the last hour | > 1 (≥ grace 2) | 5m / 10m | KeepLast / Error | `test_source_failing_past_grace_fires_per_source_with_unknown_gpus`, `test_cluster_source_counts_its_unreachable_gpus`, `test_source_stale_never_raises_a_stage_breach_end_to_end` |
| `ttv-collector-manager-stale` | Minutes since the manager's latest complete read (else since it was configured, or its first attempt); per manager that is configured or read and not removed, plus `(no collector reads)` when there is none | > 60 | 15m / 0 | NoData / Error | `test_collector_manager_stale_per_manager_and_never_read`, `test_collector_manager_stale_ignores_removed_and_counts_configured` |
| `ttv-unknown-share-high` | 100 × UNKNOWN GPUs ÷ all GPUs in open intervals; one instance (no row when the ledger has none) | > 10 | 30m / 0 | NoData / Error | `test_unknown_share_high` |
| `ttv-unknown-share-growing` | UNKNOWN share now minus 1 h ago, in percentage points; one instance (no row without 1 h of history) | > 5 | 10m / 0 | KeepLast / Error | `test_unknown_share_growing_compares_now_with_an_hour_ago`, `test_unknown_share_growing_needs_an_hour_of_history` |
| `ttv-handover-overdue` | GPUs in open `HANDOVER_PENDING` intervals flagged `handover_overdue`; per collector manager; no rows unless server-scan's last read succeeded in the last 30 min | > 0 | 15m / 15m | KeepLast / Error | `test_handover_overdue_per_manager_only_while_serverscan_is_read` |
| `ttv-node-not-found` | GPUs in open `NODE_NOT_FOUND` intervals; per cluster (kube sources attempted in the last hour, plus any cluster with such intervals) | > 0 | 30m / 15m | KeepLast / Error | `test_node_not_found_per_cluster` |
| `ttv-stage-objective-breached` | GPUs in open IDLE intervals past `stage_entered_at + objective`; per stage owner with open intervals (else one `(no idle GPUs)` row at 0); no rows unless an ok/partial engine scan finished in the last 15 min and server-scan is within grace | > 0 | 1h / 1h | KeepLast / KeepLast | `test_stage_breach_one_instance_per_owner_with_overdue_gpu_hours`, `test_next_action_annotation_never_moves_the_breach`, `test_lower_bound_stage_clock_marks_gpu_hours`, `test_no_idle_gpus_resolves_through_a_zero_row_not_nodata`, `test_stale_engine_or_serverscan_holds_stage_breach_state`, `test_source_stale_never_raises_a_stage_breach_end_to_end` |

Overdue GPU-hours in the stage-breach description sum every IDLE interval of the same stage episode (same asset, stage and `stage_entered_at`) after its deadline. Each interval uses the objective of its own policy version, as on Queues. With the §17 fixture (8 GPUs idle for 3 h, 1 h objective), the rule reports 8 overdue GPUs and 16 overdue GPU-hours.
