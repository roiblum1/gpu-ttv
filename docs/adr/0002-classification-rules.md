# ADR-0002: Classification rules as implemented

- **Status:** accepted 2026-10-30
- **Context:** AGENT.md §20.2, amended by the user's decisions in `docs/discovery.md` (C3, C6, C7) and by [ADR-0008](0008-drop-dc-stage-and-collector.md)
- **Code:** `src/ttv/classify.py` (server-level rows), `src/ttv/gpu_rows.py` (rows 15-22), `src/ttv/cluster_view.py` (node and DCGM indexes). All pure: no I/O.
- **Updated 2026-10-31:** slice 2 (target clusters, rows 11-22) implemented; additions marked *(added)*.
- **Updated 2026-11-25 (ADR-0008):** the DC stage (`PRE_PROVISIONING`) and rows 3-5 (collector-only assets) were removed. Rows 6a, 6b and the row-13 hardware case are Redbull's. Row numbers are unchanged, so 3-5 are unused.
- **Updated 2026-11-25 (ADR-0009):** five teams. Row 6a (maintenance) is NOT_CHARGED ("in repair"). Rows 16-22 changed: Run:ai and every other non-serving workload are Machine Learning, judged by GPU activity (rows 16, 16c, 16d, 20b, 21); served models are Apply's, judged by requests read from each vLLM pod (rows 16b-20); empty GPUs are Samurai's (row 22). `OTHER_WORKLOAD` is gone.

Rules are evaluated per asset in order; the first match wins. A server-level match assigns all `E` GPUs (expected GPUs from server-scan's `hardware.gpus`). If none matches, the GPU-level rows split `E` across buckets. Per asset, bucket GPU counts always sum to `E`.

## Stages and default owners

| Stage | Default team (configurable) |
| --- | --- |
| `PROVISIONING` | Redbull |
| `GPU_PLATFORM` | POPAI |
| `EMPTY` | Samurai |
| `SERVING` | Apply |
| `MACHINE_LEARNING` | Machine Learning |

A policy that still names `PRE_PROVISIONING` in `teams` or `stage_objectives` is refused at startup (ADR-0008), and so is one without all five stages (ADR-0009).

## Server-level rows

| # | Reason | Condition | Category | Stage | Slice |
| --- | --- | --- | --- | --- | --- |
| 1 | `EXCLUDED` | Asset in the exclusion list (I5) | NOT_CHARGED | — | 1 |
| 2 | `SOURCE_STALE` | The asset's source failed beyond `unknown_grace_scans`, or server-scan marks the record `stale` | UNKNOWN | — | 1 |
| 2b | `IDENTITY_CONFLICT` | Two source records claim the same asset id | UNKNOWN | — | 1 |
| 3-5 | *removed 2026-11-25* | `PROFILE_NONE`, `OTHER_INTENDED_USE` and `HANDOVER_PENDING` classified GPU servers only the profile-less collector saw (ADR-0004). They were removed with it (ADR-0008). | — | — | — |
| 6a | `MAINTENANCE` | `maintenance.enabled`, wherever the server is. In repair, not waste (ADR-0009); `maintenance.created_at` goes in evidence | NOT_CHARGED | — | 1 |
| 6b | `HARDWARE_CRITICAL`, `BMC_UNREACHABLE` | `health.overall` is a configured critical value, or `reachable == false`, **and the server is not INSTALLED in any cluster** (C3: running work wins). For INSTALLED servers they become flags; in a target cluster whose node is observed not Ready, the hardware reason applies at row 13 (slice 2). Stage clock: Redbull's normal clock | IDLE | PROVISIONING (I8) | 1 |
| 7 | `CONTESTED` | `openshift.contested_with` is set | UNKNOWN | — | 1 |
| 8 | `NOT_IN_CLUSTER` | `lifecycle_state == AVAILABLE` (also a GPU server whose `ocp*` profile is only assigned: C1) | IDLE | PROVISIONING | 1 |
| 9 | `IN_MCE_INVENTORY` | `lifecycle_state == INSTALLED_TO_INVENTORY` (I9) | IDLE | PROVISIONING | 1 |
| 10 | `OUTSIDE_TARGET_CLUSTERS` | INSTALLED, `cluster_name` not a target cluster (I7) | NOT_CHARGED | — | 1 |
| 10b | `MEMBERSHIP_UNKNOWN` | `lifecycle_state` missing or not one of the three known values | UNKNOWN | — | 1 |
| 11 | `CLUSTER_NOT_OBSERVED` | INSTALLED in a target cluster with no entry in the clusters file | UNKNOWN | — | 1 |
| 11 | `CLUSTER_UNREACHABLE` | The cluster's kube API or Thanos failed `unknown_grace_scans` times in a row. Within grace the last good snapshot is used; a cold start with no snapshot yet carries the asset's intervals unchanged | UNKNOWN | — | 2 |
| 12 | `NODE_NOT_FOUND` | No node's cleaned name equals `clean_hostname(reported_name or name)` (server-scan's own rule, D10) | UNKNOWN (+ alert) | — | 2 |
| 12 | `IDENTITY_CONFLICT` *(added)* | Two nodes clean to the same name, or two assets claim one node (`NODE_CLAIM` conflict) | UNKNOWN | — | 2 |
| 13 | `NODE_NOT_READY`, `NODE_UNSCHEDULABLE` | Node `Ready != True` or `spec.unschedulable`. A not-Ready node whose hardware is critical or whose BMC is unreachable keeps the hardware reason (`HARDWARE_CRITICAL` or `BMC_UNREACHABLE`; trace `13 NODE_NOT_READY + 6b hardware`), still Redbull's with the node-Ready clock | IDLE | PROVISIONING | 2 |
| 14 | `GPU_PLATFORM_NOT_READY` | `A` (allocatable GPU resource) is 0, or `R` (distinct DCGM UUIDs on the node) is 0 | IDLE | GPU_PLATFORM | 2 |

## GPU-level rows (slice 2)

`A` = allocatable GPUs; `R` = distinct GPU UUIDs reporting DCGM for the node (D2).

| # | Reason | GPUs | Category | Stage |
| --- | --- | --- | --- | --- |
| 15 | `GPU_MISSING` | `E − min(A, R)` if > 0. Evidence: `A`, `R`, last non-zero XID per GPU | IDLE | GPU_PLATFORM |
| 15b | `HOLDER_UNRESOLVED` *(added)* | Held by a pod whose owner chain could not be read to the top (403, deleted owner, unknown kind) | UNKNOWN | — |
| 16 | `RUNAI_WORKLOAD` | Held by a Ready (or starting) pod whose top owner is in the `run.ai` group, any kind, workspaces and build jobs included (ADR-0009), whose GPUs were busy | PRODUCTIVE | — |
| 16c | `ML_WORKLOAD` *(ADR-0009)* | The same for any other pod that is not a served model (Kubeflow, Jobs, bare pods, …) | PRODUCTIVE | — |
| 16d | `ACTIVITY_UNOBSERVED` *(ADR-0009)* | A Machine Learning pod past `warmup_grace` with neither DCGM activity nor rated power data. Unknown is never zero | UNKNOWN | — |
| 16b | `TRAFFIC_UNOBSERVED` *(added)* | A served model, Ready or starting, with zero requests counted but a count that is not complete (a pod's `/metrics` unreadable, or history too short to prove zero) | UNKNOWN | — |
| 17 | `SERVING_IN_USE` | A served model whose requests over `demand_window` (or since it started) reach `useful_requests_per_gpu_hour` per GPU-hour, wherever it runs (C7). A lower rate that can't be judged yet (count incomplete, model younger than `recent_window`, or the threshold off) is flagged `low_use_unverified` | PRODUCTIVE | — |
| 18 | `LOW_USE` | Requests above 0 but below `useful_requests_per_gpu_hour` (1 per GPU per hour, chosen 2026-11-25, I2 provisional) over a complete count; or enough requests while the deployment's GPUs averaged under `power.threshold` (25%) of rated power over `demand_window` (`basis: power`, ADR-0009) | IDLE | SERVING |
| 19 | `NO_REAL_DEMAND` | A served model, Ready or starting, with a complete count of zero requests | IDLE | SERVING |
| 20 | `MODEL_NOT_READY` *(ADR-0009)* | A served model's pod not Ready beyond `warmup_grace` (crash loop, stuck model load) while its deployment had no requests | IDLE | SERVING |
| 20b | `WORKLOAD_NOT_READY` | A Machine Learning pod not Ready beyond `warmup_grace` | IDLE | MACHINE_LEARNING |
| 21 | `LOW_GPU_UTILIZATION` *(ADR-0009)* | A Machine Learning pod older than `allocated_idle.window` (6 h) whose highest GPU activity in that window stayed below `allocated_idle.threshold` (5%) **and** whose GPUs never reached `power.threshold` (25%) of rated power; either signal alone when the other is missing | IDLE | MACHINE_LEARNING |
| 22b | `POWER_WITHOUT_POD` *(ADR-0009)* | Healthy GPUs no pod holds whose DCGM series (no pod label) reached `power.threshold` of rated power in `allocated_idle.window`, minus the GPUs of holders DCGM could not attribute: used by something Kubernetes doesn't show | UNKNOWN | — |
| 22 | `NO_WORKLOAD` | Healthy GPUs (`min(A, R, E)`) that no pod holds | IDLE | EMPTY |

How the GPU rows are applied *(added)*:
- **Holders.** Every pod bound to the node that requests the GPU resource is a holder. `holder_ref` is the pod's top owner as `namespace/Kind/name` (a bare pod is `namespace/Pod/name`); pod names go in evidence, so a pod restart does not close the interval.
- **Order.** A Run:ai top owner makes a pod Machine Learning first, even with a vLLM image. Otherwise a recognised serving kind makes it a served model (rows 16b-20). Every other pod is Machine Learning (rows 16c-21).
- **Serving recognition** comes from the policy's `serving.kinds`: top-owner group and kind (`serving.kserve.io/LLMInferenceService`, `serving.kserve.io/InferenceService`, `apps/Deployment` with a `vllm` image).
- **Requests (ADR-0009).** Each scan the engine reads `serving.metric` (`vllm:request_success_total`, all labels added) from every Running pod of a served model that holds GPUs, through the kube API pod proxy, and skips pods whose `llm-d.ai/role` is `prefill` (they count each request a second time; fact 48). Readings are kept in `serving_counter_samples` for `demand_window` + 1 h. A deployment's requests in a window are its pods' counter rises, gone pods included, with a drop or a new restart counted as a reset (`src/ttv/demand.py`). The count is complete when every pod has a reading before the window, started inside it, or still reads 0 with no restart; otherwise it is a lower bound.
- **Requests per GPU-hour** = requests / (`G` × covered hours), with `G` the GPUs the deployment holds across the cluster, and covered hours the window or the deployment's age, whichever is shorter.
- **Traffic attribution.** Pod counters can't tell synthetic traffic apart, so every serving bucket is flagged `user_attribution_unverified` (§7, D9).
- **Warm-up.** A not-Ready pod younger than `warmup_grace` (or with no start time yet) is treated as starting: a served model is judged by its requests, and a Machine Learning pod without activity data yet is in use. Beyond the grace it is row 20 or 20b.
- **Strict row order for serving pods.** Requests > 0 → row 17 or 18, even if this pod is not Ready; then not Ready beyond grace → row 20; then an incomplete zero → row 16b; then row 19.
- **Run:ai fractions (D7).** A reservation pod (in `reservation_namespaces`) takes the class of the fractional pods sharing its `runai-gpu-group` on the node (the most productive one wins), using the reservation pod's GPU activity when a fraction has none; with no fraction it is `NO_WORKLOAD`.
- **Allocation order.** Row 15 takes its GPUs first, then holders in row order (productive first), then row 22 takes what is left. When holders claim more GPUs than are healthy, the shortfall comes off the lowest-precedence holders and the trace says so. GPU models are dealt to buckets in sorted order, so a mixed-model server still yields one bucket per model and the counts sum to `E`.
- **GPU activity and flags.** Row 21 for Machine Learning, and the `allocated_idle` flag on row 17, need DCGM activity evidence covering the whole window (`DCGM_FI_PROF_GR_ENGINE_ACTIVE` ratio, else `DCGM_FI_DEV_GPU_UTIL` / 100) below the threshold, and a pod at least as old as the window; no data is never idle. Activity is read per pod, so under MIG it is relative to the pod's slice, not the parent GPU (fact 31; D4's parent-GPU rule is not applied yet). Hardware flags (C3) ride on every bucket.
- **Pools and teams.** `pool` is the first `pools` entry whose node selector matches. `team` is set on productive buckets from `team_map` (workload, then namespace), else `unassigned`.
- **Stage clocks.** Redbull's normal clock (rows 6b, 8 and 9) is `openshift.claim_changed_at` if set, else server-scan's `created_at` as a lower bound: a server's pipeline starts when it first appears in server-scan, and earlier time is unknown (ADR-0008). Row 6a has no stage. Row 13 dates Redbull's episode from the node's `Ready` `lastTransitionTime` as a lower bound, with or without a hardware reason. Rows 14-22 have no start evidence, so a new episode starts at first observation (`≥`).
- **Clocks only date new assets.** The classifier's stage clock is used only for a server the ledger has never seen. For a known server the ledger keeps the episode while the stage stays the same, across reason changes and UNKNOWN gaps, so a hardware fault outside a cluster continues Redbull's episode; maintenance closes it, because it has no stage. A move into another stage starts a new episode at that scan (`ledger.py` `_stage_clock`).
- **Milestones.** `assigned_cluster` (exact from `claim_changed_at`, else a lower bound), `node_ready`, `gpu_ready`, `workload_running` and `first_genuine_request`, each first-time only.

## Assets with no GPUs

A server-scan record whose `hardware.gpus` is empty is not a GPU asset and gets no buckets. A record whose GPU inventory is unread (server-scan lists `hardware.gpus` in `unread_fields`; at `defe172` the list itself is never null) is counted in the coverage gauge `ttv_gpu_inventory_unread_servers`, never as zero GPUs, and keeps any intervals it already has. A GPU without a model name is recorded with no model, so it is unpriced rather than priced as a model called "".

## Invariants (tested: `tests/test_classify.py`, `tests/test_classify_cluster.py`, `tests/test_ledger_plan.py`, `tests/test_engine_clusters_pg.py`)

- Per asset, bucket GPU counts sum to `E`; each GPU has exactly one category.
- UNKNOWN and NOT_CHARGED buckets are never valued.
- Flags never change a category.
- A server with GPUs of several models gets one bucket per model.
- No interval key appears twice for one asset (buckets sharing a key are merged).
- An 8-GPU node with 2 GPUs held by Run:ai training is 2 PRODUCTIVE + 6 IDLE (§6 "Capacity granularity"), and 2 h of it is 12 idle GPU-hours (§17).
