# ADR-0009: Five teams, requests read from each vLLM pod, and manager dashboards

- **Status:** accepted 2026-11-25 by the user, who owns AGENT.md. Amends ADR-0002 (rows 6a, 16-22), ADR-0006 (dashboards and alerts) and ADR-0008 (maintenance). AGENT.md (revision 5) records the decision in §0.
- **Context:** feedback the user got on the first deployment: the dashboards were too complicated for the senior managers they go to, and request counts depended on a ServiceMonitor per model that the user can't promise.
- **Code:** `src/ttv/gpu_rows.py` (rows 16-22), `src/ttv/classify.py` (row 6a), `src/ttv/demand.py` (requests from counter readings), `src/ttv/sources/pod_metrics.py` (the pod-proxy read), `src/ttv/engine.py` (`_serving_demand`, `_workload_rows`), `migrations/0007_pod_requests_and_workloads_now.sql`, `grafana/dashboards/`, `deploy/gpu-cluster-rbac/` (`pods/proxy`). Tests: `tests/test_classify_cluster.py`, `tests/test_demand.py`, `tests/test_pod_metrics_source.py`, `tests/test_engine_clusters_pg.py`.

## Context

The user asked, on 2026-11-25:

> "I want Redbull Waste - Popai Waste - Samurai Waste and Apply Waste and Home Dashboard summary this all. Right now the scraping is relying on the ServiceMonitor of vLLM, which I can't promise. I want the exporter to check inside the vLLM pods. There are many [GPUs] held by other workloads that are neither Run:ai nor serving, which I want to give a name because it's not a problem but a feature. […] I care more about current GPU nodes without anything than a non-useful workload. This should be a managers' flash board. […] If a model is being served or not should be understandable from 1 h / 24 h total requests."

Then: "merge all of the OTHER workload into Machine Learning; we can understand if they need the GPU from the GPU utilization", and "GPU servers with maintenance inside server-scan are not waste; they have a hardware problem".

Asked follow-up questions, the user chose:
- **Samurai** is charged for empty GPUs; **Apply** for served models with too few requests and for models that don't start.
- **Machine Learning** is a fifth team with its own page. It holds Run:ai (all kinds, workspaces included) and every other non-serving workload, and is judged by GPU utilization.
- Requests are read **through the kube API pod proxy** (`get` on `pods/proxy`).
- The waste threshold for a served model: "you pick a number by which we can understand whether this model on this GPU is well used".
- The headline is **GPUs and GPU-hours, no money**. *Changed on 2026-11-26:* "I want to see the money cost"; asked which money, the user chose the **AWS price** (ADR-0003's AWS On-Demand comparison) over an internal cost per GPU-hour, which the platform does not have.
- The 10 old dashboards are **deleted**.
- **Only maintenance** is "in repair". Critical hardware health and an unreachable BMC stay Redbull's waste.

## Decision

**Five stages, each charged to one team** (names are policy text):

| Stage | Team | Holds idle GPUs while… | Waste rows |
| --- | --- | --- | --- |
| `PROVISIONING` | Redbull | the server is in server-scan but is not yet a working node in a target cluster | 6b, 8, 9, 13 |
| `GPU_PLATFORM` | POPAI | the node is in the cluster but its GPUs can't be used | 14, 15 |
| `EMPTY` | Samurai | healthy, ready GPUs have nothing running on them | 22 |
| `SERVING` | Apply | a served vLLM model gets too few requests, or doesn't start | 18, 19, 20 |
| `MACHINE_LEARNING` | Machine Learning | a Run:ai or other workload holds GPUs it barely uses, or doesn't start | 20b, 21 |

**Maintenance is in repair.** Row 6a is NOT_CHARGED with no stage, wherever the server is. That also settles ADR-0008's open question about maintenance outside the target clusters.

**Machine Learning is judged by GPU activity.** A pod whose top owner is in the `run.ai` group is Machine Learning even when it runs vLLM; so is every pod that isn't a recognised served model. It is waste (row 21) when the pod is older than 6 h and its GPUs were never above 5% busy during the last 6 h (`allocated_idle`, the rule the old flag used). Without DCGM data it is UNKNOWN (row 16d), never in use or idle by default; a pod younger than `warmup_grace` gets the benefit of the doubt. The C6 split of Run:ai workspaces and build jobs into "idle by kind" is dropped.

**Requests come from each vLLM pod.** Each scan the engine reads `/metrics` of every Running pod of a served model that holds GPUs. The read is a GET to `/api/v1/namespaces/<ns>/pods/<scheme>:<pod>:<port>/proxy/metrics`, with the reader token. The engine tries `http` first, then `https`, and remembers which worked. It adds every `vllm:request_success_total` series, and skips `prefill` pods (fact 48). The port is the container port named `metrics`, else the vLLM container's first TCP port, else 8000; lab fact L2 is plain HTTP on 8000. The Thanos serving-counter queries and the D8 counter-of-record mapping are removed: no ServiceMonitor is needed.

**1 h and 24 h counts.** Readings are kept in `serving_counter_samples` for 25 h, also in dry run (operating state, like `source_state`), and reloaded at start. A deployment's requests in a window are its pods' counter rises, pods that are gone included. The count is complete when each pod has a reading from before the window, started inside it, or still reads 0 with no restart. Anything else is a lower bound (`≥`). So right after the first install an old model reads `≥ N` until a day of readings exists, but one whose counter is still 0 is known to have had no requests.

**The served-model threshold is 1 request per GPU per hour** over 24 h, or since the model started if it is younger (`useful_requests_per_gpu_hour: 1`, AGENT.md I2, provisional). The user asked for a number managers can read. Below it, a model's GPUs are idle in any practical sense: a vLLM replica serves thousands of requests an hour, and one request an hour per GPU is the most lenient line that still catches the models nobody uses. A low rate isn't judged while the count is incomplete or the model is younger than 1 h; it then counts as in use, flagged `low_use_unverified`. Zero requests are judged at once, because zero over a complete count is a fact.

**GPU power is a second sign of work** (added 2026-11-26). The user asked: "Can we read also the power usage of the GPUs and understand by that […] if this server is in use?", and for served models "power usage + amount of requests to the pods in 24 h, because sometimes the server serves but is not consumed well enough". The engine reads `DCGM_FI_DEV_POWER_USAGE` per GPU and pod: its peak over 6 h and its average over 24 h. It expresses each as a share of the GPU's rated power: the DCGM power limit (`DCGM_FI_DEV_POWER_MGMT_LIMIT`) when exported, else `power.ratings` by DCGM `modelName` (data-sheet values, the lower one where a name covers several boards), else no power for that GPU (discovery D13). The line is **25% of rated power** (`power.threshold`); an idle H100 draws about 70-120 W of its 700 W, even with a model loaded.
- **Machine Learning (row 21):** waste only when the GPUs were never above 5% busy **and** never reached 25% of rated power in 6 h. A pod with no activity data is judged by power alone, so fewer pods are UNKNOWN.
- **Served models (row 18):** enough requests, but GPUs averaging under 25% of rated power over 24 h, is still `LOW_USE` ("barely used: requests come in but the GPUs mostly sit idle"), with `basis: power` in evidence.
- **Empty GPUs (row 22b, `POWER_WITHOUT_POD`):** a GPU no pod holds whose DCGM series reached 25% in 6 h is in use by something Kubernetes doesn't show. It is UNKNOWN, not Samurai's waste. GPUs of holders that DCGM could not attribute to a pod are subtracted first.

**`workloads_now`** holds every GPU-holding workload as of the last writing scan: its requests (1 h, 24 h, complete or not), its rate, its highest GPU activity, and its status. The Apply and Machine Learning pages read it; the ledger stays the source of every GPU-hour.

**Dashboards.** Six pages replace the ten: Home, then Redbull, POPAI, Samurai, Apply and Machine Learning. Each page shows GPUs wasted now, GPU-hours wasted in the period, the trend, the reasons in plain words, and a table of what is wasting. Money is shown as the **cost of waste at AWS prices** (since 2026-11-26): the period's wasted GPU-hours and the GPUs wasted now, at one present-day snapshot (the engine's `pricing_selection`, ADR-0003), with the price list's date. It is labelled every time as what the same GPUs would cost to rent on AWS on-demand, a comparison and not our bill. GPU-hours without a price make it "at least $X", and nothing priced shows "No price", never $0. No other money view is shown or added to it. Raw reason codes are never shown; each maps to a plain label. Home puts Samurai first, because the user cares most about empty GPUs. Unknown is shown as "No data", never folded into zero.

## Consequences

- **A new permission in every target cluster.** The reader ClusterRole gains `get` on `pods/proxy` (`podMetrics.enabled`, default true). It allows a GET to any port of any pod, so some security teams will review it. Without it, every served model is "No data" (UNKNOWN) and the `vllm:<cluster>` source is marked failed with the fix in its error.
- **Old policies are refused.** A policy without `EMPTY` and `MACHINE_LEARNING`, a serving kind with `counters`, or a `runai` block with `productive_kinds`, `interactive_priority_classes` or `priority_label` fails validation (engine) and rendering (chart 0.3.0), with a message naming the change.
- **History.** Ledger rows under earlier policy versions keep their reasons (`OTHER_WORKLOAD`, `MAINTENANCE` as IDLE) and owners (`Serving`). New rows follow this ADR. GPU-hours in a period that spans the change mix both, which the dashboards show as they were recorded.
- **What is lost.** EPP-level counters and their synthetic-traffic filter (D9 `fairness_id`) are gone, so every served model is flagged `user_attribution_unverified`. A request served between a pod's last reading and its deletion is not counted (at most one scan interval).
- **Evidence.** Everything above is `fixture-tested`. Not yet run on a cluster: the pod-proxy read of a real vLLM pod (http or https), the size of `/metrics` on a busy model, the DCGM `modelName` strings and power limit on production GPUs, and the dashboards with real data.

## Open questions for the user

- **I1:** the five stage objectives (Redbull 2 d, POPAI 1 d, Samurai 1 d, Apply 7 d, Machine Learning 1 d) are provisional; the manager pages show waste, not overdue time.
- **I2:** 1 request per GPU per hour, 5% busy for 6 h, and 25% of rated power are this ADR's choices; change them in the policy.
