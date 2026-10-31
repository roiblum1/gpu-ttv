# ADR-0002: Classification rules as implemented

- **Status:** accepted 2026-10-30
- **Context:** AGENT.md §20.2, amended by the user's decisions in `docs/discovery.md` (C3, C6, C7, simplified collector)
- **Code:** `src/ttv/classify.py` (server-level rows), `src/ttv/gpu_rows.py` (rows 15-22), `src/ttv/cluster_view.py` (node and DCGM indexes). All pure: no I/O.
- **Updated 2026-10-31:** slice 2 (target clusters, rows 11-22) implemented; additions marked *(added)*.

Rules are evaluated per asset in order; the first match wins. A server-level match assigns all `E` GPUs (expected GPUs from inventory). If none matches, the GPU-level rows split `E` across buckets. Per asset, bucket GPU counts always sum to `E`.

## Stages and default owners

| Stage | Default team (configurable) |
| --- | --- |
| `PRE_PROVISIONING` | DC |
| `PROVISIONING` | Redbull |
| `GPU_PLATFORM` | POPAI |
| `SERVING` | Serving |

## Server-level rows

| # | Reason | Condition | Category | Stage | Slice |
| --- | --- | --- | --- | --- | --- |
| 1 | `EXCLUDED` | Asset in the exclusion list (I5) | NOT_CHARGED | — | 1 |
| 2 | `SOURCE_STALE` | The asset's source failed beyond `unknown_grace_scans`, or server-scan marks the record `stale` | UNKNOWN | — | 1 |
| 2b | `IDENTITY_CONFLICT` | Two source records claim the same asset id | UNKNOWN | — | 1 |
| 3 | `PROFILE_NONE` | Collector-only GPU server with no bound profile. Stage clock: the collector's first sighting, a lower bound | IDLE | PRE_PROVISIONING | 3 |
| 4 | `OTHER_INTENDED_USE` | Collector-only GPU server whose bound profile name does not match `collector.ocp_profile_pattern` (`^ocp`, case-sensitive `re.search`, as server-scan; I6) | NOT_CHARGED | — | 3 |
| 5 | `HANDOVER_PENDING` | Collector-only GPU server with an OCP profile, not (yet) in server-scan. Past `collector.handover_grace` (6 h) from the first sighting of that profile, flagged `handover_overdue` for the data-quality alert | UNKNOWN | — | 3 |

Collector-only assets *(added, slice 3)*: a server from the collector's latest complete read of each manager whose `<vendor>:<serial>` server-scan does not list (server-scan wins for the same vendor and serial). For them, row 2 means the manager's latest complete read is older than `TTV_COLLECTOR_STALE_AFTER` (1 h). A serial reported by two managers, or twice by one, is a `COLLECTOR_DUPLICATE` conflict (row 2b). A server whose GPU inventory the manager could not read is carried and counted, never zero. Collector-only assets are retired only after `tombstone_after` of absence while every manager's latest read is fresh and complete.
| 6a | `MAINTENANCE` | `maintenance.enabled` | IDLE | PRE_PROVISIONING | 1 |
| 6b | `HARDWARE_CRITICAL`, `BMC_UNREACHABLE` | `health.overall` is a configured critical value, or `reachable == false`, **and the server is not INSTALLED in any cluster** (C3: running work wins). For INSTALLED servers they become flags; in a target cluster whose node is observed not Ready, 6b applies there (slice 2). | IDLE | PRE_PROVISIONING | 1 |
| 7 | `CONTESTED` | `openshift.contested_with` is set | UNKNOWN | — | 1 |
| 8 | `NOT_IN_CLUSTER` | `lifecycle_state == AVAILABLE` (also a GPU server whose `ocp*` profile is only assigned: C1) | IDLE | PROVISIONING | 1 |
| 9 | `IN_MCE_INVENTORY` | `lifecycle_state == INSTALLED_TO_INVENTORY` (I9) | IDLE | PROVISIONING | 1 |
| 10 | `OUTSIDE_TARGET_CLUSTERS` | INSTALLED, `cluster_name` not a target cluster (I7) | NOT_CHARGED | — | 1 |
| 10b | `MEMBERSHIP_UNKNOWN` | `lifecycle_state` missing or not one of the three known values | UNKNOWN | — | 1 |
| 11 | `CLUSTER_NOT_OBSERVED` | INSTALLED in a target cluster with no entry in the clusters file | UNKNOWN | — | 1 |
| 11 | `CLUSTER_UNREACHABLE` | The cluster's kube API or Thanos failed `unknown_grace_scans` times in a row. Within grace the last good snapshot is used; a cold start with no snapshot yet carries the asset's intervals unchanged | UNKNOWN | — | 2 |
| 12 | `NODE_NOT_FOUND` | No node's cleaned name equals `clean_hostname(reported_name or name)` (server-scan's own rule, D10) | UNKNOWN (+ alert) | — | 2 |
| 12 | `IDENTITY_CONFLICT` *(added)* | Two nodes clean to the same name, or two assets claim one node (`NODE_CLAIM` conflict) | UNKNOWN | — | 2 |
| 13 | `NODE_NOT_READY`, `NODE_UNSCHEDULABLE` | Node `Ready != True` or `spec.unschedulable`. A not-Ready node whose hardware is critical or whose BMC is unreachable is row 6b instead (DC) | IDLE | PROVISIONING | 2 |
| 14 | `GPU_PLATFORM_NOT_READY` | `A` (allocatable GPU resource) is 0, or `R` (distinct DCGM UUIDs on the node) is 0 | IDLE | GPU_PLATFORM | 2 |

## GPU-level rows (slice 2)

`A` = allocatable GPUs; `R` = distinct GPU UUIDs reporting DCGM for the node (D2).

| # | Reason | GPUs | Category | Stage |
| --- | --- | --- | --- | --- |
| 15 | `GPU_MISSING` | `E − min(A, R)` if > 0. Evidence: `A`, `R`, last non-zero XID per GPU | IDLE | GPU_PLATFORM |
| 15b | `HOLDER_UNRESOLVED` *(added)* | Held by a pod whose owner chain could not be read to the top (403, deleted owner, unknown kind) | UNKNOWN | — |
| 16 | `RUNAI_WORKLOAD` | Held by a running (or warming-up) pod whose top `run.ai` owner is a Training, Distributed, Inference or DistributedInference workload, or a RunaiJob without an interactive priority class (C6) | PRODUCTIVE | — |
| 16b | `TRAFFIC_UNOBSERVED` *(added)* | Held by a serving workload for which no counter of record has series. Unknown is never zero: missing telemetry is not "no demand" | UNKNOWN | — |
| 17 | `SERVING_IN_USE` | Held by a vLLM / KServe / llm-d workload whose counter of record moved in `demand_window`, wherever it runs (C7). Before I2 is approved any genuine request qualifies, flagged `low_use_unverified`. | PRODUCTIVE | — |
| 18 | `LOW_USE` | Genuine requests above 0 but below the approved I2 threshold in requests per GPU-hour (inactive until I2 is approved) | IDLE | SERVING |
| 19 | `NO_REAL_DEMAND` | Serving pod Ready (or within `warmup_grace`) with zero requests over `demand_window` | IDLE | SERVING |
| 20 | `WORKLOAD_NOT_READY` | Held by a pod that is not Ready beyond `warmup_grace` (crash loop, stuck model load) | IDLE | SERVING |
| 21 | `OTHER_WORKLOAD` | Held by any other pod, including Run:ai workspaces and `build`-priority RunaiJobs (C6) | IDLE | SERVING |
| 22 | `NO_WORKLOAD` | Healthy GPUs (`min(A, R, E)`) that no pod holds | IDLE | SERVING |

How the GPU rows are applied *(added)*:
- **Holders.** Every pod bound to the node that requests the GPU resource is a holder. `holder_ref` is the pod's top owner as `namespace/Kind/name` (a bare pod is `namespace/Pod/name`); pod names go in evidence, so a pod restart does not close the interval.
- **Serving recognition** comes from the policy's versioned `serving.kinds`: top-owner group and kind (`serving.kserve.io/LLMInferenceService`, `serving.kserve.io/InferenceService`, `apps/Deployment` with a `vllm` image). Each kind lists **counter-of-record candidates** in order with the pods that carry them; the first candidate with series for that deployment wins and candidates are **never added**. Lab evidence: llm-d EPP v0.10.0 exports the same requests as both `llm_d_epp_request_total` and `inference_objective_request_total` (discovery.md, lab cluster). LLMInferenceService falls back to the decode/both vLLM counter and never counts `-kserve-prefill` pods (fact 48).
- **Requests per GPU-hour** = `increase(counter[demand_window]) / (G × window hours)`, with `G` the GPUs the logical deployment holds across the cluster.
- **Traffic attribution.** A counter without a synthetic-traffic filter (`matchers`, e.g. on EPP `fairness_id`) flags its buckets `user_attribution_unverified` (§7, D9).
- **Warm-up.** A not-Ready pod younger than `warmup_grace` (or with no start time yet) is treated as starting: a serving pod is judged by demand (rows 17-19), a Run:ai pod stays productive. Beyond the grace it is row 20.
- **Strict row order for serving pods.** Row 17 precedes row 20, so a serving pod is judged by its deployment's demand first: traffic unknown → `TRAFFIC_UNOBSERVED`; requests > 0 → row 17/18; zero → row 19 if Ready or warming, else row 20.
- **Run:ai fractions (D7).** A reservation pod (in `reservation_namespaces`) takes the class of the fractional pods sharing its `runai-gpu-group` on the node (the most productive one wins); with none it is `NO_WORKLOAD`. Reservation pods are never `OTHER_WORKLOAD`.
- **Allocation order.** Row 15 takes its GPUs first, then holders in row order (productive first), then row 22 takes what is left. When holders claim more GPUs than are healthy, the shortfall comes off the lowest-precedence holders and the trace says so. GPU models are dealt to buckets in sorted order, so a mixed-model server still yields one bucket per model and the counts sum to `E`.
- **Flags.** `allocated_idle` on rows 16-17 needs DCGM activity evidence covering the whole window (`DCGM_FI_PROF_GR_ENGINE_ACTIVE` ratio, else `DCGM_FI_DEV_GPU_UTIL` / 100) below the threshold, and a pod at least as old as the window; no data is never idle. Activity is read per pod, so under MIG it is relative to the pod's slice, not the parent GPU (fact 31; D4's parent-GPU rule is not applied yet). Hardware flags (C3) ride on every bucket.
- **Pools and teams.** `pool` is the first `pools` entry whose node selector matches. `team` is set on productive buckets from `team_map` (workload, then namespace), else `unassigned`.
- **Stage clocks.** Row 13 dates Redbull's episode from the node's `Ready` `lastTransitionTime` as a lower bound. Rows 14-22 have no start evidence, so a new episode starts at first observation (`≥`).
- **Milestones.** `assigned_cluster` (exact from `claim_changed_at`, else a lower bound), `node_ready`, `gpu_ready`, `workload_running` and `first_genuine_request`, each first-time only.

## Assets with no GPUs

A server-scan record whose `hardware.gpus` is empty is not a GPU asset and gets no buckets. A record whose GPU inventory is unread (server-scan lists `hardware.gpus` in `unread_fields`; at `defe172` the list itself is never null) is counted in the coverage gauge `ttv_gpu_inventory_unread_servers`, never as zero GPUs, and keeps any intervals it already has. A GPU without a model name is recorded with no model, so it is unpriced rather than priced as a model called "".

## Invariants (tested: `tests/test_classify.py`, `tests/test_classify_cluster.py`, `tests/test_classify_collector.py`, `tests/test_engine_clusters_pg.py`, `tests/test_engine_collector_pg.py`)

- Per asset, bucket GPU counts sum to `E`; each GPU has exactly one category.
- UNKNOWN and NOT_CHARGED buckets are never valued.
- Flags never change a category.
- A server with GPUs of several models gets one bucket per model.
- No interval key appears twice for one asset (buckets sharing a key are merged).
- An 8-GPU node with 2 GPUs held by Run:ai training is 2 PRODUCTIVE + 6 IDLE (§6 "Capacity granularity"), and 2 h of it is 12 idle GPU-hours (§17).
