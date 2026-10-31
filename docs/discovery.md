# Phase 0 Discovery (AGENT.md §20.10)

Status: **in progress**, started 2026-10-28. No product code is written until this document is complete and reviewed with the user.

## How to read this document

Every fact carries one evidence label (AGENT.md §3.6):

| Label | Meaning here |
| --- | --- |
| `documented` | Read in vendor documentation (URL, version, retrieval date) or in server-scan's code/docs at commit `defe172` (path:line). |
| `fixture-tested` | Exercised by an automated test against a sanitised capture. |
| `lab-tested` | Observed on a non-production system, e.g. the sandbox server-scan (demo data). |
| `production-verified` | Observed on the production estate (from the user's probe output). |

## Inputs from the user

| Date | Input | Effect |
| --- | --- | --- |
| 2026-10-28 | Repo root is `GPU-Waste-monitor/` (AGENT.md §20.9 calls it `gpu-ttv/`). | Layout built here. |
| 2026-10-28 | Managers that may hold GPU servers without a profile: **UCS Central** and **OpenManage Enterprise**. | Profile-less collector scope for R1; Intersight and standalone UCS Manager are out of scope until stated otherwise. |
| 2026-10-28 | Serving stacks: **plain vLLM Deployments** and **KServe / llm-d**. No NVIDIA Dynamo. | Counters of record are chosen among vLLM and llm-d/KServe only. |
| 2026-10-28 | Run:ai is installed on **some** target clusters, not all. | Run:ai kinds are discovered per cluster; absence is a valid result. |
| 2026-10-28 | Production is air-gapped; the user runs read-only probes and pastes sanitised output. | Live facts arrive through the probe runbook below. |
| 2026-10-28 | **Resolves a Section 0 contradiction:** server-scan ingests a server once its `ocp*` profile is *assigned*, applied or not (see Managers). Decision: **server-scan presence wins**; such a server is Redbull's (§20.2 row 8). | DC's `PROFILE_ASSIGNED_NOT_APPLIED` / `PROFILE_APPLY_FAILED` cover only servers server-scan cannot see. Redbull's queue can include time waiting for an `ocp*` profile to apply; documented as a known limitation. |
| 2026-10-28 | Dell "profile applied" = `ProfileState` Deployed **and** the deploy job finished Completed. | **Superseded 2026-10-30** by the simplified collector rule below. |
| 2026-10-28 | The collector reuses server-scan's manager accounts (not dedicated read-only ones). | Read-only is enforced in the collector's code only. server-scan's iDRAC account has Configure Manager rights (ADR-0050), so the iDRAC fallback must use GET only. |
| 2026-10-28 | **Resolves C3:** running work wins over server health (see Contradictions). | §20.2 row 6 amended before code. |
| 2026-10-28 | **Resolves C6:** Run:ai workspaces (`InteractiveWorkload`) and `build`-priority RunaiJobs are **idle**, like notebooks. | Classify by the top `run.ai` owner kind (D6): Training, Distributed and Inference workloads and non-interactive RunaiJobs are productive (row 16); interactive ones are `OTHER_WORKLOAD` (Serving), holder named. |
| 2026-10-28 | **Resolves C7:** a plain vLLM Deployment or KServe service inside a Run:ai project needs **genuine traffic**. | Only native `run.ai` kinds count by presence (row 16; Run:ai InferenceWorkload keeps I10's presence rule). Other serving engines go through rows 17–19 wherever they run. |
| 2026-10-28 | Widen the per-cluster read-only ClusterRole (§20.8) to replicasets, deployments, statefulsets, jobs, LeaderWorkerSets, Kubeflow/Knative kinds and namespaces; get/list/watch only. | Needed for the `ownerReferences` walk (§20.3). The user applies it. |
| 2026-10-28 | Sanitised probe output may leave the air-gapped network as files. | I2 hourly series and other probe output are analysed here. |
| 2026-10-30 | **Simplified profile-less collector.** Find GPU servers (GPU evidence from inventory) in OpenManage Enterprise and UCS Central. **No profile** → DC (`PROFILE_NONE`). **Profile whose name is not OCP** → `OTHER_INTENDED_USE`: flagged, not charged (I6). **OCP profile** → server-scan's domain. "Has a profile" means a profile is bound to the server, in any state. | `PROFILE_ASSIGNED_NOT_APPLIED` and `PROFILE_APPLY_FAILED` are dropped from R1, along with UCS association/config/FSM state and OME `ProfileState`/deploy jobs. An OCP-profile GPU server that server-scan has not ingested after the grace period stays `HANDOVER_PENDING` (UNKNOWN + data-quality alert). |
| 2026-10-30 | **Start implementation now**, in parallel with the production probes (overrides §20.10's "no product code until Discovery is reviewed"). Architecture as presented: ttv-collector, ttv-engine, Postgres ledger, Grafana; built in vertical slices, server-scan first. | Slice 1 uses only facts already verified (server-scan at `defe172`, the sandbox). Anything the probes contradict is fixed in the slice it affects. |
| 2026-10-30 | A GPU server that is AVAILABLE in server-scan is Redbull's; once INSTALLED in a target cluster, the node and GPU rows (§20.2 rows 11–22) decide. | Matches §20.2 rows 8–22 as amended by C3, C6 and C7. |
| 2026-10-31 | **Money is AWS On-Demand cost in US dollars.** Value idle and productive GPU-hours at the AWS On-Demand per-GPU rate (Linux, shared tenancy; instance price ÷ physical GPUs, from a dated offline snapshot) and display it in **USD**. | Overrides AGENT.md §0.5's ILS display default: dashboards show USD, so no exchange rate is needed to display value. The region stays a configuration input (I3); set to us-east-1 in the next row. Pricing is slice 4. |
| 2026-10-31 | **Pricing region us-east-1** (resolves I3). Value is computed at query time from a selected, immutable, dated, offline snapshot; intervals never store money. Historical valuation uses the snapshot in force at the time, present-day valuation a selected snapshot; a price refresh never rewrites history. Unpriced or unmapped models are shown as unpriced GPU-hours, never as $0. | Built in slice 4: ADR-0003 and "AWS pricing" below. |
| 2026-10-31 | **Pricing decisions approved:** proxies A10 → A10G (g5) and bare `NVIDIA A100` → the A100 40GB rate (a lower bound); the "smallest size with ≥ 4 GPUs" rule; V100 16GB stays unpriced. **Add B300 and H100 95GB.** | Map 2026-10-31.2: B300 exact (`p6-b300.48xlarge`, $17.802/GPU-h). H100 NVL 94GB and H100 94GB (inventory "H100 95GB") are proxies at P5's H100 rate. Both names are local catalogue additions, absent from server-scan's catalogue at `defe172` and on main of 2026-10-29. |
| 2026-10-31 | Start slice 2 (target clusters). A second lab cluster is provided: OpenShift 4.22 with KServe LLMInferenceService + llm-d and vLLM on a CPU node (no GPUs). | Kube and Thanos adapters are lab-tested against it; GPU rows stay fixture-tested until a GPU cluster is read. Findings below ("Lab cluster observations"). |

## server-scan

- **Code:** `defe172` = tag `v4.43.0` = `origin/main` (2026-10-28). Paths below are relative to the repo root; §20.4's short paths sit under `backend/app/`. Every §20.4 file exists.
- **Sandbox:** a private demo instance (URL kept out of the repo). It serves fake/seeded data from the fake provider (`docs/fake-data.md`), with auth disabled. Only GET requests were sent. Times are UTC on 2026-10-28.
  - **Version:** no endpoint exposes a version or commit. `/openapi.json` `info.version` is hard-coded `"0.1.0"` (`backend/app/main.py:146-152`).
  - The sandbox's OpenAPI lists `EventType.IDRAC_RESET`, which was added in `defe172` itself, so the sandbox runs **≥ v4.43.0**. Its schema enums and fields match the code (lab-tested, `GET /openapi.json`, 01:09Z).

### 1. Which endpoint returns every §20.4 field

| Endpoint | §20.4 fields it does NOT return | Evidence |
| --- | --- | --- |
| `GET /api/v1/servers` (`ServerSummary`) | All of `identity` except `vendor`; `profile_template`; `hardware`; `created_at` | documented `backend/app/api/v1/schemas.py:66-125`; lab-tested `?page_size=200`, 01:09:49Z (18 keys) |
| `GET /api/v1/servers/rows` (`ServerRow`) | `serial_normalized`, `system_uuid`, `hardware.gpus`, `profile_template.external_id`, `claim_changed_at`, `unreachable_since`, `created_at`. `health` is flattened to the overall string. `serial` is raw. | documented `schemas.py:260-338`; lab-tested 01:10:01Z (3008 items) |
| **`GET /api/v1/servers/{id}`** (`ServerDetail`) | **none: returns every §20.4 field** | documented `schemas.py:371-469`, `api/v1/servers.py:733-777`; lab-tested (samples below) |

- **Consequence:** no list endpoint is complete. A full read is the list plus one detail GET per server: 3008 requests on the sandbox, about 0.75 s and 12.7 KB each.
- **Proposed sync:**
  - Fetch `rows` every cycle with `If-None-Match` (returns 304 when unchanged). `rows` carries every row 6–10 input except `unreachable_since` and `claim_changed_at`.
  - ~~Fetch the detail only for new ids, or for ids whose `updated_at` moved.~~ **As built (2026-10-30, `src/ttv/sources/serverscan.py`):** fetch the detail for new ids, for ids whose row hash (the row minus `last_seen_at`/`stale`) changed, or whose cached detail is older than `TTV_DETAIL_TTL`, within a per-scan budget; a cached detail is used only while its vendor and serial still match the row. `lab-tested` 2026-10-30: a cold start fetched all 3,008 sandbox details in about 75 s (concurrency 8); the next scan got 304 and finished in 3 s.
  - `revision` and `updated_at` move only when content changes. `last_seen_at` and `listed_at` are refreshed without a revision bump. The cached detail (60 s TTL, keyed by revision) can therefore show a `last_seen_at` up to 60 s old (`docs/adr/0044-ingest-write-discipline.md:39-81`).
- **Paging on `/servers`** (keyset):
  - **Parameters:** `cursor` (opaque and HMAC-bound to filters and sort; take `page.next_cursor`), `page_size` (default 50, max 200), `with_count`, `sort` (`name|serial|model|updated_at|last_seen_at|openshift_state|cluster_name|mce_name`), `sort_desc`, `search`.
  - **Filters:** any other parameter is a filter from the whitelist. Anything else returns 400 `UNKNOWN_FILTER`, so `limit` is not a parameter (`servers.py:132,390-494`; `domain/services/search.py:41-63`; `config/settings.py:63-64`).
  - **Response:** `{items, page:{next_cursor,has_more,page_size,count,count_capped}}` (`schemas.py:128-142`).
  - lab-tested: `page_size=201` → **422** `PAGE_SIZE_TOO_LARGE` (01:09:49Z).
- **`/rows`:** no paging; returns the whole fleet as `{generated_at, items}`. It sends a weak `ETag` and `Cache-Control: no-cache` (`servers.py:532-576`).
  - lab-tested 01:13:55Z: 2.19 MB raw, 238 KB gzip, 1.5 s; `If-None-Match` → 304 in 0.6 s.
- **Auth** (ADR-0034, ADR-0043):
  - **Header:** `Authorization: Bearer <token>`. The token is checked against three static env tokens, `INVENTORY_API_TOKEN_ADMIN|VIEWER|AUDITOR` (Helm `auth.apiTokens.*`). A blank value disables that token, and two equal tokens refuse to start (`settings.py:259-264,355-382`; `dependencies.py:82-129`).
  - **Issuance:** the server-scan operator sets the Helm value or Secret. There is one shared token per role, with no per-client identity and no expiry. Rotation needs a restart (`docs/adr/0034-ad-login-roles-and-api-tokens.md:85`).
  - **Use the AUDITOR token.** It is read-only (`require_admin` rejects it) and also reads events (ADR-0043:17-29).
  - **Auth is off by default** (`settings.py:207`; Helm `auth.enabled: false`, `values.yaml:1015`). Every caller is then the dev **ADMIN** (`dependencies.py:77-104`).
    - lab-tested `GET /api/v1/auth/me` → `{"login_required":false,"username":"dev","role":"ADMIN"}` (01:09:03Z).
    - Write endpoints are therefore open on the sandbox. The platform must stay GET-only by its own discipline.
  - `/metrics` and `/health/*` are unauthenticated by design (`main.py:203-211`).
- **Rate limits:** none on the API. The only throttle is on login (`settings.py:255-257`, ADR-0039). Redis caches list pages for 15 s and details for 60 s (`infrastructure/redis/cache.py:34-35`).
- **Never call `GET /servers/available`.** It is a GET, but it re-checks the server live against the vendor manager, writes through the ingest pipeline and clears caches (`servers.py:581-730`, ADR-0032). That breaks §0.4.
- **Samples**, trimmed to the §20.4 fields (lab-tested, `GET /api/v1/servers/{id}`, 01:10:36Z), in `tests/fixtures/serverscan-sandbox/` (demo data, not sanitised production captures):
  - `serverscan-AVAILABLE.json`: OPENMANAGE, dell, 2× GPU with `power_watts`, has a profile template.
  - `serverscan-INSTALLED.json`: UCS_CENTRAL, cisco, 8× GPU with `power_watts: null`, hosted cluster via MCE (`mce_name` set).
  - `serverscan-AVAILABLE-redfish-standalone.json`: vendor `standalone`, no template.
  - INSTALLED_TO_INVENTORY: **no sample exists.** The sandbox has 0 servers in that state (607 AVAILABLE, 2401 INSTALLED; `?openshift_state=…&with_count=true`, 01:11:48Z).
  - `serverscan-openapi-sandbox.json`: the full OpenAPI schema.

### 2. Events endpoint

| Item | Fact | Evidence |
| --- | --- | --- |
| Query | `server_id`, `event_type` (plain string, not validated; an unknown value returns `[]`), `actor_id`, `since` (inclusive), `until` (exclusive), `server_name`, `cursor`, `page_size` 1–200 (default 50). Newest first. A per-server variant exists: `GET /servers/{id}/events` (`cursor` and `page_size` only). | documented `api/v1/events.py:35-36,63-149`; lab-tested `page_size=201` → 422 `VALIDATION_ERROR`, `event_type=NOPE` → 0 items (01:10:55Z) |
| Shape | `{items:[{id,event_type,server_id,server_name,actor{type,id,display,role},request_id,created_at,data}], page:{next_cursor,has_more,page_size}}` | `api/v1/events_schemas.py:13-38` |
| `SERVER_CREATED` | `data = {vendor, name}` only: no serial and no source_provider. Emitted once, when ingest finds no document with the same `(vendor, serial_normalized)`. | `application/services/ingest.py:854-861,750-757` |
| `OPENSHIFT_STATE_CHANGED` | `data = {from, to, cluster_name}`. `cluster_name` is the **new** membership's cluster, so it is null for AVAILABLE and INSTALLED_TO_INVENTORY. No `mce_name`. Emitted **only when `lifecycle_state` changes**: a move from cluster A to cluster B, or an MCE change, while INSTALLED produces no event. | `application/services/openshift_membership.py:643-677` |
| Contested noise | A contested server flips every 15 min and writes two events each time | `docs/adr/0041-contested-openshift-claim.md` |
| Role | admin or auditor; a viewer gets 403 | `main.py:209-210`, `dependencies.py:150-165`, ADR-0043 |
| Retention | Weekly `tools/prune_events.py` job with `audit_retention_days=180`. **`reportOnly` defaults to true**, so nothing is deleted until the operator turns it off. Each purge writes an `AUDIT_PURGED` event. | `settings.py:96-100`; `values.yaml:519-525`; ADR-0045:16-38 |
| Sandbox | Reachable, because auth is off. **3008 `SERVER_CREATED`** events, one per server, covering all 3008 row ids; event `created_at` is within about 1.5 ms of the server's `created_at`. **0 `OPENSHIFT_STATE_CHANGED`** although 2401 servers are INSTALLED (seeded). The store holds 10 326 events in total. | lab-tested 01:10:55Z–01:11:42Z |

### 3. §20.4 fields, enums and paths at defe172

| §20.4 item | Name and type at defe172 | Status | Source |
| --- | --- | --- | --- |
| `_id` | The API field is **`id`** (`srv_<uuid4hex>`). `_id` is the Mongo-only alias and is deliberately hidden from the API. | **MISMATCH (name)** | `schemas.py:14-19`; `domain/models/server.py:57`; `utils/ids.py:14-37` |
| `name`, `model` | `str`; `str\|None` | OK | `server.py:60-63` |
| `identity.vendor/serial/serial_normalized/system_uuid` | `Vendor` enum; `str\|None`; `str` (`""` when there is no serial); `str\|None` | OK | `server.py:31-39` |
| `site_id`, `source_provider` | `str\|None`, `str\|None` (not enum-typed) | OK | `server.py:77,83` |
| `hardware.gpus[].model/serial/health/power_watts` | `list[Gpu]`; `str\|None` ×3; `float\|None` | OK (health vocabulary is mixed, see §6) | `domain/models/hardware.py:102-121,148` |
| canonical GPU names | `DEFAULT_GPU_MODELS` in `gpu_models.py`; the matcher is in `gpu_catalog.py` | OK | `domain/value_objects/gpu_models.py:20-138` |
| `profile_template.name/external_id` | `str\|None` ×2 | OK, but it holds the **template**, not the profile (see §4) | `server.py:42-51` |
| `openshift.lifecycle_state` | `AVAILABLE \| INSTALLED \| INSTALLED_TO_INVENTORY`; a retired value decodes to AVAILABLE | OK | `domain/enums/core.py:65-80`; `domain/models/openshift.py:100-125` |
| `openshift.cluster_name/mce_name/reported_name/contested_with/claim_changed_at` | `str\|None` ×4, `datetime\|None` | OK | `openshift.py:101-109` |
| `maintenance.enabled/reason` | `bool` (default False) / free-text `str\|None` | OK | `domain/models/maintenance.py:16-24` |
| `health.overall` | `UNKNOWN\|HEALTHY\|WARNING\|MAJOR\|CRITICAL`. **`CRITICAL` is a real value.** | OK | `core.py:83-106`; `domain/models/health.py:73` |
| `reachable`, `unreachable_since` | `bool` (default True), `datetime\|None`, plus `unreachable_reason` | OK | `server.py:87-89` |
| `last_seen_at`, `created_at` | `last_seen_at` is when the server's own endpoint last answered. `created_at` is the first ingest and is preserved across updates. | OK | `server.py:84-85,97`; `ingest.py:921` |
| Events path and `data.*` | Present; see §2 for the semantics | OK (with caveats) | as in §2 |
| Gauges | All 4 names exist (labels in §7); ADR-0029 exists | OK | `observability/metrics.py:70-74,120-129,215-219` |
| Name-filter settings | All 6 exist. There is no UCS-Manager-specific pattern: UCS Manager is reached only through UCS_CENTRAL. | OK | `settings.py:180-187`; `infrastructure/providers/factory.py:407-414` |

**§20.2 rows 6–10, checked against the code.**

- **Row 6, `health.overall == CRITICAL`.** This is the worst value across 9 categories. The default policies that set CRITICAL include several that do not block GPU use: `power.psu_failed_critical`, `storage.*_critical`, `storage.name_capacity_mismatch`, `network.single_link_up`, `network.all_links_down`, `connectivity.fabric_paths_down_critical`, `raid.os_mirror_missing`, `gpu.failed` and `bmc.unreachable` (`domain/services/health/health_policy_defaults.py:111,374,438,466,513,536`). They are seeded and re-synced at startup (`application/services/bootstrap.py`); the API exposes them read-only.
  - lab-tested: 1104 of 3008 sandbox servers are CRITICAL. Sampled INSTALLED servers were CRITICAL only because of `raid.os_mirror_missing` or `storage.name_capacity_mismatch` (01:10:36Z, `health.reasons`).
- **Row 6, `reachable == false`.**
  - Only OPENMANAGE, REDFISH_STANDALONE and ONEVIEW (via the probe) ever set it. Cisco collectors (UCS_CENTRAL, INTERSIGHT) never do (`docs/adr/0037-bmc-reachability-health.md:10-25`).
  - `auth_rejected` and `tls_error` also give `false`.
  - An unreachable BMC fires `bmc.unreachable`, so `overall` becomes CRITICAL (ADR-0037:39-46). Check `reachable` before CRITICAL, or `BMC_UNREACHABLE` can never win.
- **Row 7, `contested_with`.** A `str`: the other claim, which is a cluster, an MCE or `mce/<hosted-cluster>`.
  - Set when a server flips A→B→A within 1 h. Cleared after 1 h without flipping (ADR-0041).
- **Rows 8–10.** The values match exactly.
  - INSTALLED_TO_INVENTORY ⇒ `cluster_name` is null and `mce_name` is set (`openshift.py:71-77`).
  - `cluster_name` is whatever name each nodes-status job is configured with (`docs/adr/0024-openshift-cluster-membership.md:130`). It must equal the target-list strings exactly.

### 4. Name patterns: the `ocp*` rule

- **Defaults:**
  - Code: `collector_name_pattern=""`, which collects everything. The five per-type overrides default to `None` (inherit the global); an explicit `""` opts that collector out (`settings.py:180-187`).
  - Shipped config: `^ocp` (Helm `collectors.namePattern`, `deploy/helm/server-scan/values.yaml:417-427`; `.env.example:200-218`).
  - **Production value: unverified.**
- **Resolution:** `resolve_name_pattern` picks the override if it is not None. Otherwise REDFISH_STANDALONE gets `""` (exempt, `UNFILTERED_TYPES`) and every other collector gets the global (`factory.py:371-404`).
- **Matching:** Python `re.compile(p).search(name)`:
  - It is a regex search, not `match`/`fullmatch`, so it is unanchored unless the pattern contains `^`.
  - No flags are set, so it is **case-sensitive**.
  - `^ocp` ≡ "starts with `ocp`" and does not match `OCP…` (`tools/run_collector.py:156-231`; values.yaml:418).
  - **Applied to `ProviderServer.name`** (= `Server.name`) by `_NameFilteredProvider`. Some collectors also pre-prune with the same pattern.

| Collector | Pattern setting | What the pattern is tested against | Profile-less hardware | Source |
| --- | --- | --- | --- | --- |
| UCS_CENTRAL (→ each UCS Manager) | `ucs_central_name_pattern` → global | The service-profile name (`lsServer.name` via `assigned_to_dn`), else the blade's own name, else its DN. Central also skips whole domains whose known profile names never match. | Named by label or DN, so `^ocp` drops it. | `ucs_manager/mapping.py:60-75,732-749`; `ucs_central/provider.py:142-193`; `docs/cisco-collectors.md:219-232` |
| INTERSIGHT | `intersight_name_pattern` → global | `server.Profile.Name` (Associated, else Assigned), else the SP DN tail, else **UserLabel**, else Name | Kept if its UserLabel starts with `ocp` | `intersight/mapping.py:124-149`; `intersight/provider.py:111-126` |
| OPENMANAGE | `ome_name_pattern` → global | OME `ProfileName`, pre-filtered before any iDRAC is contacted | Never listed: profiles with no target are skipped | `openmanage/provider.py:334-403`; `openmanage/mapping.py:166-199` |
| ONEVIEW | `oneview_name_pattern` → global | The server-profile name | Hardware with no profile is skipped | `oneview/provider.py:353-372` |
| REDFISH_STANDALONE | `redfish_name_pattern` only; the global does not apply | The inventory `name`, else what the BMC reports | The inventory TOML is the filter | `factory.py:373,402-403`; `values.yaml:600-621` |

- **Applying it in the platform:** `profile_template.name` holds the UCS/Intersight SP template, the OME deployment template or the OneView profile template. **The `ocp` test applies to the profile name (= `Server.name`), not to the template.**
- **Comparison with §6:** `^ocp` under `re.search` with case sensitivity is the same rule as §6's "anchored `^ocp.*`".
  - The profile-less collector should read the same configured pattern string (a config input, not hard-coded).
  - Apply it with `re.search` to the UCS SP name and the OME `ProfileName`.

### 5. Serial normalisation, vendor and asset key

- **Function:** `app.domain.services.normalize.normalize_text`. It returns `" ".join(value.split()).lower()`: lower-case, outer whitespace trimmed, inner whitespace runs collapsed to one space, nothing else (dashes are kept). `None` or empty gives `""` (`domain/services/normalize.py:19-33`). Applied at ingest (`ingest.py:750`).
- **Placeholders:** `PLACEHOLDER_SERIALS` covers `to be filled by o.e.m.`, `default string`, `0123456789`, `system serial number`, `none`, `n/a`, `unknown`, `not specified` and `""` (`normalize.py:36-62`).
  - The list is applied **only** by Redfish `_clean_serial` (`redfish/mapping.py:76-89`) and the OpenShift SSH serial fallback (`infrastructure/openshift/node_serial.py:114`).
  - UCS, Intersight, OME and OneView serials pass through unchecked.
- **Dell serial = Service Tag:** OME `DeviceServiceTag`, or the iDRAC's `Oem.Dell.DellSystem.NodeID`. It is **not** the Redfish `SerialNumber`, which is a board serial (`redfish/mapping.py:92-110`). The profile-less collector must use the same value.
- **Correlation key:** `(vendor, serial_normalized)`, with a unique index that is partial: it covers only non-empty serials (`ingest.py:4-12,666-679`; `infrastructure/mongodb/indexes.py:81-85`).
  - **A server with no serial gets `serial_normalized=""`** and is treated as a new server, except standalone Redfish, which matches by BMC host (`ingest.py:681-710`). The sandbox has 3 such rows.
  - `'<vendor>:'` therefore collides. The platform needs a fallback key.
- **Vendor values:** `dell | cisco | hp | standalone`, lower-case. HPE is `hp`.
  - Redfish maps `Manufacturer` by prefix; anything else, or an absent value, becomes `standalone` (`core.py:16-27`; `redfish/mapping.py:35-73`).
  - If `Manufacturer` appears later, the machine splits into a second document (ADR-0016 update).

### 6. GPU catalogue and fidelity per provider

- **Canonical names** follow the form `"NVIDIA H100 80GB"`, `"NVIDIA A100 80GB"`, `"NVIDIA L40S 48GB"`, `"AMD Instinct MI300X 192GB"`. There are 30+ rows (`gpu_models.py:20-138`).
- **Enrichment** at ingest replaces `model` with the canonical name **only on an exact normalised match**: upper-cased, brand words dropped, separators stripped, compared for equality.
  - An unmatched or ambiguous model passes through raw, e.g. bare `NVIDIA A100` or PID `UCSC-GPU-A100-40` (`docs/cisco-collectors.md:784-817`; `ingest.py:1108-1115`).
  - The pricing map must therefore accept raw strings. *(Done: `gpu_model_aliases` maps raw to canonical names with this catalogue's rules; ADR-0003.)*
- **Unread GPUs:** if a run cannot read the GPU list, the previous list is carried forward and `hardware.gpus` is added to `unread_fields` (`ingest.py:1108-1115`).
- **`power_watts`** is a point reading taken at collection time, every 3 h by default (`values.yaml` schedules). It is excluded from change detection (`ingest.py:224-227`). It is not telemetry.

| Provider | Source of `gpus[]` | Count | Model | Serial | `health` vocabulary | `power_watts` |
| --- | --- | --- | --- | --- | --- | --- |
| UCS_CENTRAL | `graphicsCard`, equipped cards only | yes | Cisco PID → catalogue | yes | `UP/DOWN/DISABLED/UNKNOWN` | always None |
| INTERSIGHT | `graphics.Card` | yes | Model or Pid | yes | `UP/DOWN/DISABLED/UNKNOWN` | always None |
| OPENMANAGE, REDFISH_STANDALONE | Redfish `Processors` with `ProcessorType=GPU`; a DGX/HGX baseboard is merged into its host | yes | Model string | yes | `HEALTHY/WARNING/CRITICAL/UNKNOWN` | from `EnvironmentMetrics.PowerWatts` where exposed |
| Same, optional PCIe fallback (`redfish_pcie_gpu_detection=False` by default) | `PCIeDevice` | yes | PCI-ID map, else the description | None | Redfish vocabulary | None |
| ONEVIEW | `Devices` with `DeviceType=GPU`; the list is unread (None) on iLO 4 | yes | `Name` | yes | Redfish vocabulary | None |

- Sources: `ucs_manager/mapping.py:490-527`; `intersight/mapping.py:294-320`; `redfish/mapping.py:560-622,815-851`; `oneview/mapping.py:287-325`; `settings.py:173`.
- `gpu.failed` counts CRITICAL **or** DOWN, so it handles both vocabularies (`domain/services/health/metrics.py:380-384`).
- The sandbox's Cisco GPUs show HEALTHY/CRITICAL. That is the fake generator, not real behaviour.

### 7. Fleet gauges (`backend/app/observability/metrics.py`)

| Need | Metric and labels | Evidence |
| --- | --- | --- |
| Collector freshness | `server_scan_collector_last_seen_timestamp_seconds{source_provider}` = max(`last_seen_at`). Also `server_scan_collector_last_run_timestamp_seconds{source_provider}`. | `metrics.py:90-94,125-129`; ADR-0029:37 |
| Partial run | `server_scan_collector_last_run_partial{source_provider}` (1 = PARTIAL) | `metrics.py:120-124` |
| Unmatched membership | `server_scan_membership_last_run_unmatched{kind,reported_by}`; `kind` is `nodes` or `agents`. Related: `…_partial`, `…_timestamp_seconds`, `…_unresolved` | `metrics.py:195-234` |
| Stale servers | `server_scan_servers_stale{source_provider}`: `last_seen_at` older than `INVENTORY_STALE_AFTER_SECONDS` (43200 s = 12 h), or absent | `metrics.py:70-74`; `settings.py:87` |
| Also useful | `server_scan_cluster_last_reported_timestamp_seconds{cluster}`, `server_scan_openshift_contested_servers`, `server_scan_audit_oldest_event_timestamp_seconds` | `metrics.py:130-191` |

- **Exposure:** `/metrics` is served at the root, has no auth and is read-only on scrape. It refreshes from Mongo at most every 30 s (`main.py:213-237`; `observability/fleet_gauges.py:301-334`).
- **Sandbox:** `GET /metrics` returns 200 with the collector and stale series for all 5 providers (lab-tested 01:11:35Z).
  - `server_scan_membership_*` has HELP/TYPE lines but **no series**: there are no membership-run documents.
  - `/api/metrics` and `/api/v1/metrics` return 404.

### 8. `source_provider` and standalone DGX

- **`source_provider` values** in practice: `UCS_CENTRAL`, `INTERSIGHT`, `OPENMANAGE`, `ONEVIEW`, `REDFISH_STANDALONE`.
  - The `ManagerType` enum also has `UCS_MANAGER`, but it has no collector entry point of its own (`core.py:40-53`; `factory.py:407-414`).
  - The value is that of the last collector to ingest the server (`ingest.py:1168`). Seen on the sandbox (rows, 01:10:01Z).
- **Standalone DGX:** collected by `REDFISH_STANDALONE`. An NVIDIA `Manufacturer` maps to vendor **`standalone`**, so the asset key is `standalone:<serial>`.
  - "Registration" means adding a `[[hosts]]` entry, optionally with a `name`, to the Redfish inventory TOML. That file is a GitOps, review-gated ConfigMap (`deploy/helm/server-scan/files/redfish/inventory.toml`; `docs/examples/redfish-inventory.example.toml`).
  - The collector ships `enabled: false, suspend: true` and runs every 3 h (`values.yaml:617-634`).
- **No `registered_at` field.** The closest is `Server.created_at` and the matching `SERVER_CREATED` event at the first ingest. It lags the inventory edit by up to one collector run.
  - An unreachable host still gets a stub with `reachable=false` and no serial. The stub keeps its `_id` and `created_at` when it is later reached (ADR-0037 §4, decision 5; `ingest.py:681-710`).

### 9. Other findings that bear on §0 and the §20.2 inputs

- **Profile apply state is invisible to server-scan.**
  - UCS takes the profile via `assigned_to_dn`; Intersight via AssociatedServer, falling back to AssignedServer; OME via `TargetName`.
  - No collector reads or stores association or deployment state (no `assoc_state` or `DeploymentStatus` in the providers). UCSPE profiles stuck in `config-failure` were still collected (`docs/cisco-collectors.md:177-181`).
  - So an `ocp…` profile that is assigned but not applied, or that failed, **is in server-scan**.
- **Freshness:** inventory collectors run every 3 h, membership jobs every 15 min, and `stale` means 12 h. A 5-minute poll mostly sees unchanged data.
- **No "installed since" timestamp.**
  - `openshift.last_reported_at` does not advance while membership is unchanged.
  - `claim_changed_at` is set only when one claim takes a server over from another.
  - `assigned_cluster` milestones need events, which never existed for seeded data, and a lower bound.
- **Servers are never deleted** unless the prune tool is enabled (off by default) and records `SERVER_PRUNED` (`settings.py:91-94`; `tools/prune_servers.py`). When a manager stops listing a server, it only ages into `stale`.
- **The sandbox does not apply the name filter.** 317 names such as `random-server-0008` do not match `^ocp`. Name-filter behaviour therefore cannot be lab-tested here.

## Managers and the profile-less collector

> **Simplified 2026-10-30** (see Inputs): R1 uses only each server's GPU evidence and the name of its bound profile. The association, config, FSM, `ProfileState` and deploy-job material below is background research, not an R1 input.
>
> **What server-scan's code already does** (`defe172`, `documented`), which the collector reuses:
> - **Cisco:** the UCS Central collector logs in to every registered UCS Manager domain and reads each server, its bound profile and its GPUs (`graphicsCard`, `ucs_manager/mapping.py:474`). A server with no profile is named after its hardware object or DN (`mapping.py:60-75`), so only the `^ocp` name filter removes it. With the filter cleared, server-scan's own code lists every Cisco server: the collector needs no new Cisco client.
> - **Dell:** server-scan finds servers through OpenManage **profiles** and reads GPUs from each iDRAC over Redfish (ADR-0020; `ProcessorType == "GPU"`, else a GPU-vendor `PCIeDevice`, `redfish/mapping.py:556-870`). Servers with no profile never enter that loop, so the collector adds one device-driven OpenManage listing and reuses the Redfish GPU rule.
> - Production counts come from Part B of `docs/discovery-probes.md`, which runs server-scan's own collector in `--dry-run` mode with the filter cleared.

Phase 0 Discovery. Read-only; no product code. Retrieved/inspected **2026-10-28**.
**Scope (user, 2026-10-28):** GPU servers that may lack a profile live only in **UCS Central** (and its managed UCS Manager domains) and **Dell OpenManage Enterprise (OME)**. Intersight and standalone UCS Manager get one line each.

**Evidence labels.** `documented` only. Vendor docs and SDK sources give URL, version and retrieval date. server-scan code and docs are cited as `path@defe172:line`; when server-scan records its own live, UCSPE or production evidence, that provenance is quoted. Nothing here is lab-tested or production-verified by this Discovery.

**Source keys** (all retrieved 2026-10-28):
- **CSDK** — ucscsdk **0.9.0.8**, `https://raw.githubusercontent.com/CiscoUcs/ucscsdk/v0.9.0.8/ucscsdk/mometa/<pkg>/<Class>.py`. Its HISTORY.rst says it supports UCS Central 2.0(1v).
- **MSDK** — ucsmsdk **0.9.18**, `https://raw.githubusercontent.com/CiscoUcs/ucsmsdk/v0.9.18/ucsmsdk/mometa/...`. These are the versions server-scan pins (`pyproject.toml@defe172:48-58`).
- **SMG-SP / SMG-SRV** — UCS Central 2.0 Server Management Guide, chapters "Service Profiles and Templates" and "Cisco UCS Servers": `https://www.cisco.com/c/en/us/td/docs/unified_computing/ucs/ucs-central/GUI-User-Guides/Server-Mgmt/2-0/b_CiscoUCSCentral_ServerManagementGuide-2-0/` (chapter_0100 and chapter_010).
- **CLI2.0** — UCS Central CLI Reference 2.0, the §19 URL (chapter_010011).

The UCS Central XML API Programmer's Guide is no longer on cisco.com (unconfirmed / not fetched). CSDK is therefore the de-facto spec, which is also server-scan's own stance (`docs/adr/0014-…md@defe172:45-48`).

### Summary
- **The name filter.** Each collector's server list goes through `_NameFilteredProvider` (`tools/run_collector.py@defe172:156-215`). It runs `re.search(pattern, ProviderServer.name)` after the fetch.
  - UCS: the name is the **assigned** service profile's name, else the DN.
  - OME: the name is `ProfileName`.
  - The pattern is `INVENTORY_COLLECTOR_NAME_PATTERN`, with optional per-type overrides (`backend/app/config/settings.py@defe172:180-187`; `infrastructure/providers/factory.py@defe172:375-404`). Docs use `^ocp` as the example, and the deployed value is unknown.
  - Pre-fetch pruning also exists: UCS Central skips whole domains and OME skips BMCs (`docs/architecture.md@defe172:1388-1396`).
- **Cisco reuse.** With the pattern empty, server-scan's Cisco path already lists every equipped server, including those with no profile, and their real `graphicsCard` GPUs. It does **not** carry profile association, config or failure state.
- **Dell reuse.** server-scan's OME path is **profile-driven**: it iterates `/ProfileService/Profiles`. Servers with no profile can never come out of it, filter or not. A device-driven listing is required.
- **Assigned is enough for server-scan.** server-scan treats an **assigned** `ocp*` profile as qualifying, whether or not it applied. It never reads the apply state. This contradicts the Section 0.1/0.3 premise (see "Contradictions").

### UCS Central (+ its managed UCS Manager domains)

**How server-scan does it today**

| Need | API / class / field | Evidence | Source |
|---|---|---|---|
| Entry point | Central is "a directory, not an inventory source". It makes two Central queries, `computeSystem` (domains + `address`) and `lsServer` (profile names + `domain`). Each domain is then read live by `UcsManagerProvider` with one shared UCS Manager login. | documented | `docs/cisco-collectors.md@defe172:1024-1055`; `ucs_central/provider.py@defe172:441-460` |
| Per-domain listing | 14 domain-wide `query_classid` calls, joined client-side by DN. They include `computeBlade`, `computeRackUnit`, `lsServer` and `graphicsCard`. Servers are kept only if `presence` starts with `equipped` (excluding `-slave` / `-not-primary`). | documented ("validated against UCSPE 4.2(2aS9)", ADR-0009) | `ucs_manager/provider.py@defe172:205-224`; `ucs_common.py@defe172:71-91`; `docs/adr/0009-…md@defe172:275-279` |
| Profile join | `profile_by_dn[computeBlade.assignedToDn]`. Name = `lsServer.name`, else `computeBlade.name`, else the DN. | documented | `ucs_manager/mapping.py@defe172:60-75,732` |
| Where the filter runs | **Post-fetch** on the name (`_NameFilteredProvider`). Also pre-fetch **domain pruning**: a domain is skipped only when Central lists profiles for it and none match. No server-side `filter_str` is used, by design. | documented | `ucs_central/provider.py@defe172:142-190`; `docs/cisco-collectors.md@defe172:1056-1105` |
| What gets dropped | Servers with no profile, which are named by DN (`sys/chassis-3/blade-1`) and fail `^ocp`. Servers whose assigned profile is non-ocp. Every server in a pruned domain. | documented | `docs/adr/0009-…md@defe172:389-394` |
| Live scale (server-scan's run) | "Validated against a live UCS Central" on 2026-08-18: 152 domains, ~3346 equipped servers, 3613 profiles + 516 templates. **All profiles `global-controlled`, zero `localized`.** 3265/3346 servers resolved a profile name, and the 81 unresolved are "likely … no service profile assigned". 52 domains were pruned. | documented (server-scan live run) | `docs/adr/0014-…md@defe172:404-465` |
| Reusable with filter off? | **Partly.** `UcsCentralProvider(name_pattern="")` disables pruning and the wrapper, so every equipped server comes back with serial, model, GPUs and `profile_dn`. But `ProviderServer` has no `assocState`/`configState`/`assignState`/FSM fields, and the join is one-way (`assignedToDn` only). | documented | `domain/ports/provider.py@defe172:87-143`; `ucs_central/provider.py@defe172:142-190` |

**Which facts Central exposes vs which need a read-only UCS Manager session per domain**

| Need | Central's own API (CSDK 0.9.0.8) | UCS Manager (MSDK 0.9.18) | Evidence | Verdict |
|---|---|---|---|---|
| Registered domains | `computeSystem` (`compute/sys-<id>`): `id`, `name`, `address`, `inventoryStatus` (ok/throttled/in-progress), `lastRefreshedTs`, `totalPhysicalCnt`, `availablePhysicalCnt`. `extpolClient` (`extpol/reg/clients/client-<id>`) gives `operState` (registered … lost-visibility, version-mismatch), `connectionState`, `lastPollTs`. | n/a | documented (CSDK `compute/ComputeSystem.py`, `extpol/ExtpolClient.py`, `utils/ucscdomain.py`); server-scan reads `inventory_status`, `last_refreshed_ts`, `total_physical_cnt` (`ucs_central/provider.py@defe172:654-661`) | **Central** |
| All physical servers, incl. unassociated | `computeBlade` (`compute/sys-<id>/chassis-<c>/blade-<s>`), `computeRackUnit` (`compute/sys-<id>/rack-unit-<n>`), `computeServerUnit`. Fields: `serial`, `model`, `vendor`, `uuid`, `presence`, `association` (associated/establishing/failed/**none**/removing/throttled), `assignedToDn`, **`lsDn`** (Central-only), `availability`, `operState`, `lc`, `lcTs`. | Same classes under `sys/…`, plus `fsmStatus`/`fsmRmtInvErrDescr`. | documented (CSDK `compute/ComputeBlade.py`, inspected). server-scan's `tools/verify_ucs_central.py@defe172:135-177` queried Central's own `computeBlade`/`computeRackUnit` live: "REPORTED == SEEN for every one" of 152 domains (`docs/adr/0014-…md@defe172:412-414`) | **Central** (it is a polled replica, so pair it with `lastRefreshedTs`) |
| GPU inventory | **`graphicsCard` exists in CSDK 0.9.0.8**: `mometa/graphics/GraphicsCard.py`, rn `graphics-card-[id]`, parent `computeBoard`, since Central 1.4(1a), privilege `["read-only"]`. Fields: `model`, `vendor`, `serial`, `vendorId`, `deviceId`, `pciSlot`, `pciAddr`, `presence`, `operState`, `mode`. SMG-SRV: the Server Details "GPUs" tab "Displays a list of all graphics cards in the server". | `graphicsCard` (since UCSM 2.1(3a)) at `…/board/graphics-card-N`. server-scan reads it per domain (`ucs_manager/mapping.py@defe172:527-543`) and chose it over `coprocessorCard` (`docs/cisco-collectors.md@defe172:697-760`). | documented (CSDK source inspected 2026-10-28; MSDK; server-scan) | **Central has the class, but whether its replica populates it is unconfirmed** (ADR-0014's own caveat: "class definitions prove the model *can express* those objects, not that a given Central populates them", `@defe172:310-315`). UCS Manager is the proven fallback. |
| Global profile: assigned / associated / applied / failed | `lsServer` (global, under `org-root/…`): `assignState` (assigned/unassigned/failed), `assocState` (associated/associating/disassociating/failed/unassociated), `configState` (applied/applying/failed-to-apply/not-applied), `configQualifier`, `operState`, `pnDn`, `type` (instance/initial-template/updating-template), `operSrcTemplName`, `domain`. Children: `lsBinding` (`pnDn`, `assignedToDn`, `issues`), `lsRequirement` (pool, `assignedToDn`, `issues`), `lsServerFsm` (`rmtErrDescr`, `progress`), `lsSPMeta.ownershipState` (global-controlled/localized/…). | The domain's `lsServer` carries the same state plus readable `fsmStatus`/`fsmRmtInvErrDescr`. Whether a Central-owned global profile appears there 1:1 is unconfirmed. | documented (CSDK `ls/LsServer.py` enumerations inspected; MSDK identical) | **Central** for state. On Central, `fsmStatus`/`fsmRmtInvErrDescr` are `INTERNAL` props (CSDK `ls/LsServer.py:249,253`), so failure *detail* is more reliably read in UCS Manager. |
| Local (domain-owned) profiles | SMG-SP: "Local service profiles are managed by Cisco UCS Manager". The Central GUI/CLI show them (`scope local-service-profile` / `show instance`: Assoc State, Config State, Physical Ref). An XML method `configResolveDomainElements` exists in CSDK, but its semantics are undocumented (unconfirmed). | `lsServer` in the domain is authoritative. | documented (SMG-SP, CLI2.0, CSDK); server-scan's live fleet had **0 `localized`** (`docs/adr/0014-…md@defe172:415-430`) | **Needs UCS Manager** if any local profiles exist. Detect them via `lsSPMeta.ownershipState=localized` and per-domain profile counts. |
| First-discovered timestamp | None on `computeBlade`/`computeRackUnit`/`graphicsCard`/`lsServer`. The nearest are `lcTs` (last lifecycle change), `mfgTime` and `computeSystem.lastRefreshedTs`. | Same, plus `fsmStamp`. | documented (CSDK, MSDK) | **None**. The collector must record its own `first_seen` (a lower bound). |

**Reason codes (Cisco).** Join on both sides: compute → profile through `assignedToDn` or `lsDn`, and profile → compute through `lsServer.pnDn` / `lsBinding.pnDn`. Evaluate in this order:

| Reason | Condition |
|---|---|
| `PROFILE_NONE` | Equipped server; no instance `lsServer` (`type=instance`) has `pnDn` = this DN; `assignedToDn` empty; `association=none`. |
| `PROFILE_APPLY_FAILED` | Bound profile has `assocState=failed`, or `configState=failed-to-apply`, or `assignState=failed`, or `operState` like `config-failure`. Evidence: `configQualifier`, `lsServerFsm.rmtErrDescr`, `lsBinding.issues`. |
| `PROFILE_ASSIGNED_NOT_APPLIED` | Bound profile has `assocState ∈ {associating, unassociated}` or `configState ∈ {applying, not-applied}`. A pool `lsRequirement` with no binding counts too. |
| `OTHER_INTENDED_USE` | `assocState=associated`, `configState=applied`, and `re.search(pattern, lsServer.name)` is false. |
| `HANDOVER_PENDING` | Same applied state, the name matches, and the server is absent from server-scan after the grace period. |

- `disassociating` is reported as `PROFILE_ASSIGNED_NOT_APPLIED` with the raw state kept as evidence. This is a proposal for the user to confirm.
- The FSM progress fields and a lab sample for each state are a **validation gate**. server-scan's UCSPE run only ever saw `oper_state="config-failure"`, and saw `assigned_to_dn` "set regardless" (`docs/adr/0009-…md@defe172:382-386`).

**Credentials and limits (Cisco)**
- **Accounts.** server-scan uses two: a UCS Central login (`INVENTORY_UCS_CENTRAL_*`) and **one UCS Manager login that must authenticate on every registered domain** (`INVENTORY_UCS_MANAGER_*`). There is no per-domain account (`docs/test-ucs-collector.md@defe172:25-45`; `deploy/README.md@defe172:594-600`). server-scan's live run saw "zero login failures across 100 collected domains" (`docs/adr/0014-…md@defe172:397-401`).
- **Roles.** server-scan does not document the UCS role it uses. Whether the built-in `read-only` role can resolve every class is unconfirmed: `graphicsCard`'s MoMeta lists `read-only`, while `lsServer`/`computeBlade` list write privileges only. Treat it as a validation gate.
- **Extra privilege.** None is expected beyond read: the classes added (`lsBinding`, `lsServerFsm`, `lsSPMeta`, `extpolClient`) are the same tree.
- **Sessions.** "Both Central and UCS Manager enforce a per-user session cap", and an abandoned generator leaks sessions (`docs/cisco-collectors.md@defe172:982-995`). The numeric cap and timeout are unconfirmed.
- **SDK behaviour.** `UcscHandle` has no timeout, so the wrapper must impose one (`docs/cisco-collectors.md@defe172:905-940`). Port is 443 only, and the endpoint must be a bare host.
- **TLS is not verified by either SDK.** Both wrap the socket in `ssl.SSLContext(PROTOCOL_SSLv23)`, which checks neither the certificate nor the host name, and both retry any SSL error as TLSv1 (`ucscsdk` 0.9.0.8 `ucscdriver.py`, `ucsmsdk` 0.9.18 `ucsdriver.py`: `TLSConnection.connect`, `post`; `documented`, inspected 2026-10-31). server-scan configures nothing, so its Cisco sessions are unverified. The collector forces verification (ADR-0004, "Cisco (UCS Central)"). Whether Central's and each domain's certificate chains to a CA bundle and matches the address used is a validation gate.
- **Cost and cadence.** Per domain, cost is flat in server count (`@defe172:1125-1135`). A 15-minute profile-less loop with **no pruning** contacts all ~152 domains, against server-scan's 3-hourly schedule (`deploy/README.md@defe172:588`). The proposal in "Reuse recommendation" keeps that small.
- **Versions.**
  - SDKs: ucsmsdk 0.9.18 and ucscsdk 0.9.0.8, chosen because the air-gapped mirror carries them (`pyproject.toml@defe172:48-58`).
  - UCSPE: 4.2(2aS9) (ADR-0009).
  - Central schema: CSDK 0.9.0.8 targets Central 2.0(1v).
  - The deployed Central and UCS Manager versions are **not recorded** in server-scan.

### Dell OpenManage Enterprise

**Source keys** (all retrieved 2026-10-28):
- **API32** — OME 3.2 / OME-M 1.00.10 RESTful API Guide (HTML), `https://www.dell.com/support/manuals/en-us/dell-openmanage-enterprise/ome-3.2_omem-1.00.10_apiguide/`. It is the only API guide readable: the 3.5/3.10/4.x API guide PDFs on dl.dell.com returned HTTP 403.
- **ANS** — Dell's `dellemc-openmanage-ansible-modules`, branch `collections` (10.0.x; README: tested on OME 4.2/4.4/4.5 and OME-M 2.10.10+): `https://github.com/dell/dellemc-openmanage-ansible-modules/tree/collections/plugins/`. `ome_profile.py` and `ome_profile_info.py` were inspected directly.
- **OMEGH** — `https://github.com/dell/OpenManage-Enterprise` (docs/API.md, docs/python_library_code.md).
- **PSM** — `dell/OpenManage-PowerShell-Modules` `Get-OMEDeviceDetail.ps1`.
- **TF** — `dell/terraform-provider-ome` (`models/devices.go`).
- **UG40** — OME 4.0.x User's Guide, `…/dell-openmanage-enterprise/ome_p_40_users_guide/` (topics configuring-profiles and role-and-scope-based-access).
- **UG46** — OME 4.6 User's Guide, topic `monitor-graphics-processing-unit-gpu`.

**How server-scan does it today**

| Need | API / field | Evidence | Source |
|---|---|---|---|
| Listing | Two bulk calls, `GET /ProfileService/Profiles` and `GET /DeviceService/Devices` (no `Type` filter), paged via `@odata.nextLink`. Each **profile** is joined to a device by `TargetName == DeviceName`. Hardware, **including GPUs**, then comes from each iDRAC over Redfish ("OME discovers, Redfish collects"). | documented. Endpoints "validated in the production scanner"; `serverStorage` HTTP 400 seen in a "live OME run" 2026-08-18. | `openmanage/provider.py@defe172:333-388`; `docs/dell-collectors.md@defe172:22-64,76-100`; ADR-0020 |
| Where the filter runs | **Pre-fetch** on `ProfileName` (`_matches`, before any BMC is contacted), plus the post-fetch `_NameFilteredProvider` on the same name. | documented | `openmanage/provider.py@defe172:365-403` |
| What gets dropped | Every **device with no profile**: the loop iterates profiles, never devices. Profiles with no target/iDRAC address ("undeployed templates"). Non-matching profile names. **`ProfileState` and the deploy-job result are never read.** | documented | `openmanage/provider.py@defe172:347-388`; `openmanage/mapping.py@defe172:166-197` |
| Reusable with filter off? | **No** for discovery: a profile-less server cannot come out of a profile-driven loop. The `OmeClient` session and paging can be reused. | documented | as above |
| server-scan GPU extraction | iDRAC Redfish `Processors` with `ProcessorType=="GPU"` ("GPUs appear here alongside CPUs" on iDRAC). There is an opt-in `PCIeDevices` fallback (vendor ID `10DE` + display-class description), **off by default**. It exists because "a real host … reported 0 GPUs despite having 8 NVIDIA cards". | documented (server-scan live evidence) | `redfish/mapping.py@defe172:435-448,556,758-782`; `settings.py@defe172:173`; ADR-0016 `@defe172:599-640`; `docs/field-test-checklist.md@defe172:457` |

**What the profile-less collector needs from OME**

| Need | API / field | Evidence | Source |
|---|---|---|---|
| All servers, incl. profile-less | `GET /api/DeviceService/Devices?$filter=Type eq 1000`. Type map: 1000 server, 2000 chassis, 3000 storage, 4000 network IOM, 8000 storage IOM (`GET DeviceService/DeviceType`). Fields: `Id`, `Type`, `Identifier`, `DeviceServiceTag`, `ChassisServiceTag`, `Model`, `DeviceName`, `ManagedState`, `Status`, `ConnectionState`, `PowerState`, `DeviceManagement[].NetworkAddress`, `LastInventoryTime`, `LastStatusTime`. | documented (API32 get-method-for-devices; ANS `module_utils/ome.py` `get_device_type`). `Type eq 1000` is inferred from the map plus ANS's documented `Type eq 2000` example. | API32, ANS, TF |
| GPU evidence (real inventory) | `GET /api/DeviceService/Devices(Id)/InventoryTypes`, then `…/InventoryDetails('<type>')`. Known server types include `serverDeviceCards`, `serverProcessors`, `serverMemoryDevices`, `serverArrayDisks`. UG46: GPUs show under the device Hardware tab, "Graphics Processor" section, with telemetry component GPU. **The REST InventoryType behind "Graphics Processor", and the GPU field names in `serverDeviceCards`, are unconfirmed.** The fallback is iDRAC Redfish exactly as server-scan does it. | documented (types: PSM, ANS; UI: UG46). The GPU type is a **validation gate**. | PSM, ANS `ome_device_info.py`, UG46 |
| Profile ↔ server | `GET /api/ProfileService/Profiles`: `Id`, `ProfileName`, `TemplateId`, `TemplateName`, `TargetId`, `TargetName`, `ProfileState`, `DeploymentTaskId`, `LastRunStatus`, `ProfileModified`, `CreatedDate`, `LastEditDate`. In OME 3.2 the path is `/api/TemplateService/Profiles`, so it is version-dependent. | documented (ANS `ome_profile_info.py` sample, inspected; API32) | ANS, API32 |
| ProfileState | **0 = unassigned** (`ome_profile.py`: "Profile is in an unassigned state"). **4 = deployed** ("migrate applicable in deployed state only"). **1 = assigned** is inferred, because assign checks `in [1, 4]`. No official enumeration exists. UI states: "Unassigned", "Assigned to Device", "Deployed". Profiles are "created either implicitly during a template's deployment/auto-deployment" or manually. | documented (ANS `ome_profile.py` L700, L737, L848 inspected; UG40). The numbers are inferred, so they are a **validation gate**. | ANS, UG40 |
| Assignment without application | On MX7000, a slot template deployed without "Immediately Apply to Compute Sleds" leaves the profile "Assigned" and identities "Reserved", not deployed. | documented | Dell KB 000214041 (dell.com/support/kbdoc/000214041) |
| Application result | `ProfileState=4` plus the deploy job: `GET /api/JobService/Jobs(DeploymentTaskId)` → `LastRunStatus.Id`. Values: 2020 Scheduled, 2030 Queued, 2040 Starting, 2050 Running, **2060 Completed**, **2070 Failed**, 2080 New, 2090 Warning, 2100 Aborted, 2101 Paused, 2102 Stopped, 2103 Canceled. The profile's own `LastRunStatus` mirrors it (the ANS sample shows 2200, meaning unconfirmed). | documented | OMEGH python_library_code.md; ANS `ome.py` |
| Template (distinct fact) | `GET /api/TemplateService/Templates`: `ViewTypeId` 2 = Deployment, 1 = Compliance; `TypeId` is the target device type. Deploy action: `TemplateService.Deploy`. | documented | ANS `ome_template.py` |
| Drift (distinct fact) | `ProfileModified` (profile or template changed since deploy), and configuration compliance: `GET /api/TemplateService/Baselines` → `…/DeviceConfigComplianceReports` (`ComplianceStatus`, e.g. `NONCOMPLIANT`) → `…/DeviceComplianceDetails`. `/api/UpdateService/Baselines` is **firmware** compliance, which is a different thing. | documented | ANS `ome_configuration_compliance_info.py`; UG40 |
| First-discovered timestamp | **None on the device.** No `DiscoveryFirstTime` was found in API32, TF, ANS or OMEGH. Only `LastInventoryTime` and `LastStatusTime` exist. The profile has `CreatedDate`, a lower bound for "profile created", not "applied". Devices carry `DiscoveryConfigurationJobInformation` (job name), whose job history might hold a time (unconfirmed). | documented | API32, TF, ANS |

**Reason codes (Dell).** Template, profile, assignment, application and drift are kept as separate recorded facts. Evaluate in this order:

| Reason | Condition |
|---|---|
| `PROFILE_NONE` | Server device, and no profile has `TargetId == Id`. |
| `PROFILE_ASSIGNED_NOT_APPLIED` | `ProfileState=1`, or `ProfileState=4` with the deploy job in 2020/2030/2040/2050/2080/2101. |
| `PROFILE_APPLY_FAILED` | Deploy job in 2070/2100/2102/2103. |
| `OTHER_INTENDED_USE` | `ProfileState=4`, job 2060, and `re.search(pattern, ProfileName)` is false. |
| `HANDOVER_PENDING` | `ProfileState=4`, job 2060, name matches, absent from server-scan after the grace period. |

- **Needs a user decision:**
  - Whether 2090 Warning ("completed with errors") counts as applied.
  - Whether drift (`ProfileModified`, `NONCOMPLIANT`) is evidence only (the proposal) or reopens DC.
  - Which provisioning path proves Dell pre-provisioning complete: a profile, a template deploy without a profile, or something else (§4).

**Credentials and limits (Dell)**
- **OME account.** server-scan uses `INVENTORY_OME_IP/_USERNAME/_PASSWORD`; the role is not documented.
  - Viewer is "View all data", and "Administrator and Viewer roles have unrestricted scope" (UG40).
  - **Do not use Device Manager.** It is scope-limited and sees only the profiles it owns, which would turn other teams' profiles into false `PROFILE_NONE`.
  - Viewer GET on Profiles, Jobs and Baselines is implied but not stated, so it is a validation gate.
- **iDRAC account.** server-scan's single iDRAC account (`INVENTORY_OME_BMC_*`) "now needs the Configure Manager privilege" when iDRAC reset (ADR-0050, dated 2026-10-28; report-only by default) is enabled (`docs/adr/0050-…md@defe172:17-23`; `deploy/README.md@defe172:674-679`). **If Redfish is needed for GPU evidence, use a separate read-only iDRAC account. This is the only extra credential.**
- **Sessions and paging.**
  - Login is `POST /api/SessionService/Sessions` (`SessionType:"API"`, token in `X-Auth-Token`); logout is `DELETE …Sessions('<Id>')` (OMEGH; `docs/dell-collectors.md@defe172:27-34`).
  - ANS sample `SessionConfiguration` (API): MaxSessions 32, SessionTimeout 99600 ms. These are sample values, not defaults.
  - Paging uses `$top`/`$skip` or `@odata.nextLink`. The default page size is unconfirmed. No rate limit is documented.
  - **Per-device `InventoryDetails` is one call per server**, so run it on new or changed devices only and refresh it slowly.
- **Versions.**
  - Deployed OME version: **unknown**. server-scan does not record it.
  - Live facts in server-scan are for iDRAC9 ("several live iDRAC9 servers"; R660 iDRAC9 BIOS 2.3.5).
  - ANS is tested on OME 4.2–4.5. The profile path differs between OME 3.2 and later versions.

### Intersight and standalone UCS Manager (out of scope)
- **Intersight.**
  - server-scan lists `compute/PhysicalSummaries`, `server/Profiles` (preferring `AssociatedServer` over `AssignedServer`) and `graphics/Cards` (real inventory, identity only).
  - The name filter is post-fetch, and auth uses an API key with a Read-Only role (`intersight/provider.py@defe172:323-408`; `docs/cisco-collectors.md@defe172:1281-1300,1387-1395`; `deploy/helm/server-scan/values.yaml@defe172:925-933`).
- **Standalone UCS Manager.** There is no entry point. It was removed 2026-08-17, and `UcsManagerProvider` now runs only per domain under UCS Central. A domain not registered with Central is "uncollectable" (`factory.py@defe172:407`; `docs/adr/0014-…md@defe172:271,368-373`).

### Identity join (drop servers already in server-scan)
- **Key.** Match on `(identity.vendor, identity.serial_normalized)`:
  - `vendor` ∈ {`cisco`, `dell`} (`domain/enums/core.py@defe172:16-27`).
  - `serial_normalized = normalize_text(serial)`, which lowercases and collapses whitespace (`domain/services/normalize.py@defe172:19-33`; applied at `application/services/ingest.py@defe172:750`).
  - SMBIOS placeholder serials count as no serial (`normalize.py@defe172:36-62`).
  - Reuse this pure function verbatim.
- **Cisco serial.** It is `computeBlade/RackUnit.serial` (`ucs_manager/mapping.py@defe172:751`). Central's replica carries the same `serial` property (CSDK).
- **Dell serial.** server-scan stores the Service Tag:
  - It reads iDRAC Redfish `Oem.Dell.DellSystem.NodeID`, "settled against … several live iDRAC9 servers". It is *not* `SerialNumber`.
  - It equals OME `DeviceServiceTag` (`docs/dell-collectors.md@defe172:117-137`). The unreachable stub stores OME's `DeviceServiceTag` directly (`openmanage/mapping.py@defe172:193`).
  - So OME `DeviceServiceTag` is the right join key.

### Reuse recommendation

**Recommendation: option (b)+(c).** Vendor-copy server-scan's clients and pure helpers at commit `defe172`, using the same SDK pins, and write new, small listing and mapping code for profile state. Do **not** use option (a), depending on server-scan as a package.

**Why not (a), a pinned package dependency:**
1. **Namespace and dependency clash.** The wheel's top-level import name is the generic `app` (`pyproject.toml@defe172:123-124`). It drags in the whole service's exact pins: fastapi, pymongo, redis, ldap3, asyncssh, pydantic==2.13.4 (`@defe172:9-66`).
2. **Wrong output shape.** The provider classes output `ProviderServer`, which has no association, config or failure fields (`domain/ports/provider.py@defe172:87-143`). The OME provider cannot list profile-less servers at all.
3. **Too heavy for a 15-minute loop.** The Cisco path issues 14 queries per domain, including storage, NIC and PSU queries the collector does not need.
4. **Ships write-capable code.** The package includes the iDRAC reset path (`redfish/idrac_reset.py`, ADR-0050), which a Section 0.4 read-only collector should not carry.

**Coupling is low, so copying is cheap.** No provider module imports the settings singleton, Mongo or repositories. They import `app.domain.models.manager.Manager` (pydantic), `ManagerConnection` (a dataclass), `ProviderServer`, `structlog` and `app.infrastructure.blocking.run_abandonable` (stdlib only, 93 lines). The settings and name-pattern resolution live in `factory.py` and `tools/run_collector.py`.

**What to copy verbatim:** each file gets a provenance header (repo, `defe172`, path), plus a test that re-diffs it against server-scan at the pinned commit.
- `domain/services/normalize.py`: `normalize_text`, `is_placeholder_serial` (identity join).
- `domain/value_objects/gpu_models.py` and `gpu_catalog.py`: friendly names and VRAM. These are enrichment only and never decide GPU presence.
- `infrastructure/providers/ucs_common.py`: `is_equipped`, `group_by_owning_server_dn`, `partition_profiles`.
- `infrastructure/blocking.py`, plus the timeout and poisoning wrappers in `ucs_manager/client.py` and `ucs_central/client.py`. These are hard-won hardening, per `docs/cisco-collectors.md@defe172:944-981`.
- `openmanage/client.py`: session, `X-Auth-Token`, `@odata.nextLink`.
- The **semantics** of `resolve_name_pattern` (an override wins even when empty, then the global) and `re.search`. Read the **same configured values** server-scan runs with.

**What to write new (thin):**
- **Cisco.** One UCS Central session per cycle: `computeSystem`, `extpolClient`, `computeBlade`, `computeRackUnit`, `lsServer`, `lsBinding`, `lsSPMeta`, `graphicsCard`.
  - Join both ways and classify.
  - Open a read-only UCS Manager session for a domain **only** when one of these holds:
    - Central's `graphicsCard` replica is empty for that domain while `totalPhysicalCnt > 0`.
    - A profile there is `localized`.
    - Failure detail (`fsmRmtInvErrDescr`) is needed.
  - Run the per-domain GPU sweep on a slow loop (e.g. 3 h, plus on new serials). Central is a polled replica, so record `lastRefreshedTs` per domain as observation freshness.
- **Dell.** Device-driven listing (`Type eq 1000`), joined to Profiles by `TargetId`, then Jobs for `DeploymentTaskId`.
  - GPU evidence comes from OME `InventoryDetails`, cached per device and refreshed slowly.
  - If the live probe shows OME exposes no GPU inventory, fall back to **copying** server-scan's Redfish GPU functions (`gpus_from_processors`, `is_gpu_pcie_device`, `gpus_from_pcie_devices`) with a read-only iDRAC account. The target is `DeviceManagement[0].NetworkAddress`.
  - **As built (2026-10-31, `src/ttv/collector/openmanage.py`, `fixture-tested`):** both paths are in: OME `InventoryDetails('serverDeviceCards')` first, iDRAC Redfish (HTTP Basic, GET only) when OME lists no GPU. No Jobs are read (simplified rule). Binding, gates and enforcement: ADR-0004, "Dell (OpenManage Enterprise)".

## Target GPU clusters

Phase 0 Discovery (AGENT.md §20.10). There was no live cluster access: the only kube context's token had expired, and `ocp4-prod-gpu-site1..N` are air-gapped. Every fact below is `documented`, meaning read in official docs or upstream source and **not** observed on our clusters. Nothing here is lab- or production-verified. Every row was retrieved on **2026-10-28**. Live confirmation comes from the runbook below.

**Source keys** (prefix + path = full URL):
- Run:ai: **RAI** `https://run-ai-docs.nvidia.com/` · **RAIL** `https://d161wck8lc3ih2.cloudfront.net/` (legacy docs.run.ai preview builds, July 2024, ~2.17–2.18) · **KAI** `https://github.com/NVIDIA/KAI-scheduler/blob/v0.13.0/` (open-sourced Run:ai scheduler; the one shipped with a Run:ai release may differ).
- NVIDIA: **GO** `https://github.com/NVIDIA/gpu-operator/blob/<tag>/` · **DE** `https://github.com/NVIDIA/dcgm-exporter/blob/<tag>/` · **NVD** `https://docs.nvidia.com/datacenter/`.
- OpenShift: **OD** `https://github.com/openshift/openshift-docs/blob/enterprise-4.20/modules/` (AsciiDoc source of docs.redhat.com, which returned 403 to automated fetch; same text on 4.16 unless stated) · **CMO** `https://github.com/openshift/cluster-monitoring-operator/blob/release-4.20/assets/` · **OSG** `https://github.com/openshift/` · **ODH** `https://github.com/opendatahub-io/opendatahub-documentation/blob/main/modules/`.
- Serving: **VL** `https://github.com/vllm-project/vllm/blob/v0.32.0/` · **LR** `https://github.com/llm-d/llm-d-router/blob/v1.0.0/` · **LD** `https://github.com/llm-d/llm-d/blob/v1.0.0/` · **GIE** `https://github.com/kubernetes-sigs/gateway-api-inference-extension/blob/<tag>/` · **KS** `https://github.com/kserve/kserve/blob/v0.21.0/` · **KSM** `https://github.com/kubernetes/kube-state-metrics/blob/e85f093/`.
- Inventory: **SS** server-scan `github.com/team-redbull/server-scan` at commit `defe172`.

### Documented facts

| # | Fact | Value | Evidence | Source · version · date |
|---|---|---|---|---|
| 1 | Run:ai native workload kinds | `InteractiveWorkload`, `TrainingWorkload`, `DistributedWorkload`, `InferenceWorkload`, all in `run.ai/v2alpha1` | documented | RAIL `PR-893/developer/cluster-api/workload-overview-dev` · ~2.17 · 2026-10-28 |
| 2 | Workspace and third-party mapping | A "Workspace" is `InteractiveWorkload`. Third-party jobs (PyTorchJob, MPIJob, LWS…) are tracked as `ExternalWorkload` objects named `ew-…`, in namespace `runai-<project>` | documented | RAI `api/2.24/api-guides/interacting-with-workloads-using-the-cluster-api.md` · 2.24 · 2026-10-28 |
| 3 | Legacy kind | `run.ai/v1 RunaiJob`; the Researcher k8s API is deprecated. A RunaiJob labelled `priorityClassName: build` "is equivalent to … `--interactive`" | documented | RAIL `PR-874/developer/deprecated/k8s-api/launch-job-via-yaml` · legacy · 2026-10-28 |
| 4 | Kinds the scheduler groups | `RunaiJob`, plus `run.ai` Training/Interactive/Distributed/Inference/**DistributedInference**Workload as skip-top-owner kinds. Also kubeflow PyTorchJob/MPIJob/TFJob/Notebook, Knative Service, LeaderWorkerSet, JobSet, Ray | documented (source) | KAI `pkg/podgrouper/podgrouper/hub/hub.go` · v0.13.0 · 2026-10-28 |
| 5 | Scheduler CRDs are a separate group | `scheduling.run.ai`: Queue v2, PodGroup v2alpha2, BindRequest v1alpha2. `--api-group=run.ai` does not list them | documented (source) | `https://pkg.go.dev/github.com/kai-scheduler/api` · KAI · 2026-10-28 |
| 6 | Cluster config and install namespace | CRD `runaiconfigs.run.ai` (`runaiconfig runai -n runai`); the cluster is installed in project `runai` | documented | RAI `self-hosted/2.22/infrastructure-setup/advanced-setup/cluster-config` · 2.22 · 2026-10-28 |
| 7 | Platform support | Run:ai 2.22 supports OCP 4.15–4.19, x86 only. Inference needs Knative (OpenShift Serverless), distributed needs the Kubeflow Training Operator, distributed inference needs LWS ≥ 0.7 | documented | RAI `self-hosted/2.22/getting-started/installation/support-matrix`, `…/install-using-helm/system-requirements` · 2.22 · 2026-10-28 |
| 8 | Project ↔ namespace | Default namespace `runai-<project>`; an existing namespace is linked with label `runai/queue=<project>`. Pods carry `runai/queue` and `schedulerName: runai-scheduler` | documented | RAIL `PR-1222/admin/runai-setup/cluster-setup/project-management`; RAI `self-hosted/2.20/platform-management/runai-scheduler/scheduling/default-scheduler` · 2.20 · 2026-10-28 |
| 9 | Pod grouping | Annotation `pod-group-name` points to the PodGroup. The pod-grouper reads labels `project`, `priorityClassName`, `user`; priority classes are `build`, `train`, `inference` | documented (source) | KAI `pkg/podgrouper/podgrouper/plugins/constants`, `…/runaijob/runaijob-grouper.go` · v0.13.0 · 2026-10-28 |
| 10 | UI types and categories | Workspace, Training, Distributed Training, Inference, "Job (legacy)"; categories Build / Train / Deploy | documented | RAI `self-hosted/2.22/workloads-in-nvidia-run-ai/workloads` · 2.22 · 2026-10-28 |
| 11 | Third-party types by category | Build: DevWorkspace, Job, Notebook, TaskRun. Train: PyTorchJob, MPIJob, TFJob, JAXJob, Ray, JobSet. Deploy: Deployment, StatefulSet, ReplicaSet, Pod, Service, InferenceService, LWS, NIMService. Run:ai has **no native kind named "deployment"** | documented | RAI `saas/workloads-in-nvidia-run-ai/workload-types/supported-workload-types` · SaaS · 2026-10-28 |
| 12 | Default priorities | Workspace high (non-preemptible), training low, inference very-high | documented | RAI `self-hosted/2.22/platform-management/runai-scheduler/scheduling/workload-priority-control` · 2.22 · 2026-10-28 |
| 13 | Fractional request | Pod annotations `gpu-fraction`, `gpu-memory`, `gpu-fraction-num-devices`, `gpu-fraction-container-name`. The pod has **no** `nvidia.com/gpu` | documented | RAI `self-hosted/2.22/platform-management/runai-scheduler/resource-optimization/fractions`; `self-hosted/2.24/…/submit-via-yaml` · 2.22/2.24 · 2026-10-28 |
| 14 | Reservation pod | "a pod in a dedicated runai-reservation namespace with the full GPU". Fractions and MIG never share a node | documented | same fractions page · 2.22 · 2026-10-28 |
| 15 | Reservation internals | Requests `nvidia.com/gpu: 1`, is pinned by `spec.nodeName`, and has annotation `run.ai/reserve_for_gpu_index`. Label `runai-gpu-group=<id>` is also on the fractional pods (multi-device uses prefix `runai-gpu-group/`). The pod is deleted when its group is empty | documented (source) | KAI@main `pkg/binder/binding/resourcereservation/resource_reservation.go` · main · 2026-10-28 |
| 16 | Standalone KAI | Uses namespace `kai-resource-reservation` and label `kai.scheduler/queue`, and installs `scheduling.run.ai` CRDs **without** `run.ai` | documented (source) | `https://pkg.go.dev/github.com/NVIDIA/KAI-scheduler/pkg/common/constants` · v0.13.0 · 2026-10-28 |
| 17 | Dynamic MIG | Deprecated in 2.19 and "no longer supported" | documented | RAI `guides/2.20/platform-management/aiinitiatives/resources/mig-profiles` · 2.20 · 2026-10-28 |
| 18 | Run:ai metrics | `runai_allocated_gpu_count_per_workload{workload_type,workload_name,…}`, `runai_allocated_gpu_count_per_pod`. Direct Prometheus use "will be deprecated" (2.17) | documented | RAIL `PR-893/developer/metrics/metrics` · 2.17 · 2026-10-28 |
| 19 | GPU Operator → exporter tag | 24.9.2→3.3.9-3.6.1; 25.3.0→4.1.1-4.0.4; 25.3.1/.2→4.2.3-4.1.3; 25.3.3/.4→4.3.1-4.4.0; 25.10.0→4.4.1-4.6.0; 25.10.1→4.4.2-4.7.0 | documented (source) | GO@vX `deployments/gpu-operator/values.yaml` · per tag · 2026-10-28 |
| 20 | Metrics file the operator uses | `DCGM_EXPORTER_COLLECTORS=/etc/dcgm-exporter/dcp-metrics-included.csv`, not `default-counters.csv` | documented (source) | GO@v24.9.2 `assets/state-dcgm-exporter/0900_daemonset.yaml`; GO@v25.10.1 `…/0800_daemonset.yaml` · 2026-10-28 |
| 21 | Collected by default | `POWER_USAGE`, `TOTAL_ENERGY_CONSUMPTION` (mJ counter), `GPU_UTIL`, `MEM_COPY_UTIL`, `XID_ERRORS`, `FB_FREE/USED`, remapped-rows fields, `PROF_GR_ENGINE_ACTIVE`, `PROF_PIPE_TENSOR_ACTIVE`, `PROF_DRAM_ACTIVE`, `PROF_PCIE_TX/RX_BYTES` | documented (source) | DE@3.3.9-3.6.1…4.4.2-4.7.0 `etc/dcp-metrics-included.csv` · 2026-10-28 |
| 22 | **Not** collected by default | `ECC_SBE/DBE_VOL_TOTAL`, `ECC_SBE/DBE_AGG_TOTAL`, `PROF_SM_ACTIVE`, `SM_OCCUPANCY`, `FP64/32/16` (commented out) | documented (source) | same file · all four tags · 2026-10-28 |
| 23 | Custom fields | Only through ClusterPolicy `spec.dcgmExporter.config.name`: a ConfigMap in the operator namespace with key `dcgm-metrics.csv` | documented (source) | GO@v25.10.1 `controllers/object_controls.go` L1744-1764 · 2026-10-28 |
| 24 | Series labels | `gpu`, `UUID`, `pci_bus_id`, `device`, `modelName`, `Hostname`; MIG adds `GPU_I_PROFILE`, `GPU_I_ID`; the driver version appears as a label | documented (source) | DE@4.4.2-4.7.0 `internal/pkg/rendermetrics/render_metrics.go` L42-53 · 2026-10-28 |
| 25 | `Hostname` = node name | Taken from env `NODE_NAME`, which the operator sets from `spec.nodeName`; `os.Hostname()` is used only if it is unset. DaemonSet `hostNetwork` is false (24.9–25.10) | documented (source) | DE@4.4.2-4.7.0 `internal/pkg/hostname/hostname.go`; GO@v25.10.1 `object_controls.go` L1699-1708 · 2026-10-28 |
| 26 | Workload attribution | pod/namespace/container labels need `DCGM_EXPORTER_KUBERNETES=true` (the operator sets it) and the kubelet pod-resources socket. They appear only on GPUs allocated via `nvidia.com/gpu` or `nvidia.com/mig-*` | documented (source) | DE@4.4.2-4.7.0 `internal/pkg/transformation/kubernetes.go`, `pkg/cmd/app.go` · 2026-10-28 |
| 27 | Time-slicing | "DCGM-Exporter does not support associating metrics to containers when GPU time-slicing is enabled" (the last pod wins) | documented | NVD `cloud-native/gpu-operator/latest/gpu-sharing.html` · latest · 2026-10-28 |
| 28 | DCGM ServiceMonitor | `nvidia-dcgm-exporter`, created when `dcgmExporter.serviceMonitor.enabled` (true in the OLM example). Port `gpu-metrics`, `/metrics`, `jobLabel: app`. **No honorLabels and no relabelings** unless set in ClusterPolicy | documented (source) | GO@v25.10.1 `assets/state-dcgm-exporter/0500_service_monitor.yaml`, `object_controls.go` L4759-4830 · 24.9–25.10 · 2026-10-28 |
| 29 | Workload labels as scraped | NVIDIA's OpenShift console plugin queries `DCGM_FI_PROF_GR_ENGINE_ACTIVE{exported_pod=~".+"}` by `exported_namespace, exported_pod` | documented (source) | `https://github.com/rh-ecosystem-edge/console-plugin-nvidia-gpu/blob/main/src/components/GPUDashboard/Cards/WorkloadsCard.tsx` L122 · main · 2026-10-28 |
| 30 | Where DCGM lands | The operator labels `nvidia-gpu-operator` with `openshift.io/cluster-monitoring=true` if the label is absent; other namespaces must be labelled manually. Metrics go to **platform** Prometheus | documented | NVD `cloud-native/openshift/latest/install-gpu-ocp.html` ("Cluster monitoring"); GO@v25.10.1 `controllers/state_manager.go` L62-63 · 2026-10-28 |
| 31 | Profiling availability | Datacenter GPUs from Volta on. Metrics are multiplexed, and values are blank while profiling is paused for Nsight. Under MIG, `GR_ENGINE_ACTIVE` per GPU instance is relative to that instance's slice | documented | NVD `dcgm/latest/user-guide/feature-overview.html` · DCGM latest · 2026-10-28 |
| 32 | Field semantics | `XID_ERRORS` = value of the last XID (a gauge, not a counter). `GPU_UTIL` is 0–100 %; `GR_ENGINE_ACTIVE` is a 0–1 ratio. DCGM 4.7 renames fields and keeps aliases | documented | `https://github.com/NVIDIA/DCGM/blob/v4.4.1/dcgmlib/dcgm_fields.h` L743-746; DE CSV comments · 2026-10-28 |
| 33 | Thanos querier scope | Aggregates platform and user-defined-project metrics. CMO adds the user-workload Prometheus and Thanos Ruler as endpoints | documented | OD `monitoring-default-monitoring-components.adoc`; `https://github.com/openshift/cluster-monitoring-operator/blob/release-4.20/pkg/manifests/manifests.go` · 4.16/4.20 · 2026-10-28 |
| 34 | Route and ports | Route `openshift-monitoring/thanos-querier` exposes only `/api`, to port 9091 (reencrypt). 9091 serves all endpoints and needs `cluster-monitoring-view`. 9092 is per-namespace (query, range, labels, series; `?namespace=`). 9093 serves rules and alerts | documented | OD `monitoring-resources-reference-for-the-cluster-monitoring-operator.adoc`; CMO `thanos-querier/route.yaml` · 4.16–4.20 · 2026-10-28 |
| 35 | Authentication | Bearer token only. Red Hat's example uses `TOKEN=$(oc whoami -t)` and the route host from `.status.ingress[].host`. 9091 authorises `prometheuses/api` `k8s`, which `cluster-monitoring-view` grants | documented | OD `monitoring-about-accessing-monitoring-web-service-apis.adoc`, `accessing-metrics-outside-cluster`; CMO `thanos-querier/kube-rbac-proxy-web-secret.yaml` · 4.16–4.20 · 2026-10-28 |
| 36 | API load and timeouts | "Limit queries to a maximum of one every 30 seconds". The Thanos query timeout is the 2 m default. IngressController `serverTimeout` is 30 s by default | documented | OD `accessing-monitoring-apis-by-using-the-cli.adoc`, `nw-ingress-controller-configuration-parameters.adoc`; CMO `thanos-querier/deployment.yaml` · 4.20 · 2026-10-28 |
| 37 | **Retention defaults** | Platform Prometheus **15 d**; **user-workload Prometheus 24 h**; Thanos Ruler 24 h. Set through `user-workload-monitoring-config` → `prometheus.retention` | documented | OD `monitoring-retention-time-and-size-for-prometheus-metrics-data.adoc` · 4.16/4.20 · 2026-10-28 |
| 38 | Service-account tokens (4.16+) | Token secrets are no longer auto-created. A long-lived token is a Secret of `type: kubernetes.io/service-account-token` with annotation `kubernetes.io/service-account.name` | documented | OD@enterprise-4.16 `service-account-auto-secret-removed`, `nodes-pods-secrets-creating-sa` · 4.16 · 2026-10-28 |
| 39 | RHOAI serving metrics | "KServe does not generate its own metrics"; runtime metrics (`vllm:*`) need user-workload monitoring. RHOAI setup sets UWM retention to 15 d ("recommended value") | documented | ODH `configuring-monitoring-for-the-model-serving-platform.adoc`, `viewing-metrics-for-the-model-serving-platform.adoc` · main · 2026-10-28 |
| 40 | Auto-created monitor | odh-model-controller creates the `<isvc>-metrics` ServiceMonitor for RawDeployment ISVCs, in the ISVC namespace (so UWM). The Tech Preview observability stack (`monitoring.opendatahub.io/scrape=true`) uses its own Prometheus, not Thanos | documented (source) | `https://github.com/opendatahub-io/odh-model-controller` (main, 2026-10-27) `internal/controller/serving/reconcilers/kserve_metrics_servicemonitor_reconciler.go`; ODH `collecting-metrics-from-user-workloads.adoc` · main · 2026-10-28 |
| 41 | vLLM request counter | `vllm:request_success_total{model_name, engine, finished_reason}`; `finished_reason` ∈ stop, length, abort, error, repetition (each pre-created at 0) | documented (source) | VL `vllm/v1/metrics/loggers.py` L499-505, L807-819; `vllm/v1/engine/__init__.py` L36-66 · v0.32.0 · 2026-10-28 |
| 42 | vLLM other series | `vllm:num_requests_running`, `vllm:num_requests_waiting`, `vllm:prompt_tokens_total`, `vllm:generation_tokens_total`, `vllm:kv_cache_usage_perc`; histograms `e2e_request_latency_seconds`, `time_to_first_token_seconds`, `inter_token_latency_seconds` | documented (source) | VL `loggers.py` L528-1104; `docs/design/metrics.md` · v0.32.0 · 2026-10-28 |
| 43 | When it increments | Only when the engine reports a request finished. Queue-admission rejections go to `vllm:admission_rejections_total`. `--disable-log-stats` removes the `vllm:*` engine metrics | documented (source) | VL `loggers.py` L847-858, L1289; `vllm/v1/engine/output_processor.py` L805-830; `async_llm.py` · v0.32.0 · 2026-10-28 |
| 44 | Probes and labels | `/health`, `/ping`, `/load`, `/metrics` create no engine request. vLLM counters have **no client or tenant label** | documented (source) | VL `vllm/entrypoints/serve/instrumentator/health.py`, `metrics.py` L64-73 · v0.32.0 · 2026-10-28 |
| 45 | vLLM naming churn | A metric deprecated in X.Y is hidden in X.Y+1 and removed in X.Y+2. `gpu_cache_usage_perc` is gone from v0.12 (use `kv_cache_usage_perc`) | documented | VL `docs/usage/metrics.md` L132-136; `loggers.py`@v0.10.0–v0.12.0 · 2026-10-28 |
| 46 | llm-d EPP counter | `llm_d_epp_request_total{model_name,target_model_name,fairness_id,priority}`, incremented once per request after scheduling succeeds; `/v1/models` is not counted. Rate query: `sum by(model_name)(rate(llm_d_epp_request_total[5m]))` | documented | LR `docs/metrics.md` L106-129, `pkg/epp/handlers/server.go` L597-651; LD `docs/operations/observability/promql.md` L22 · v1.0.0 · 2026-10-28 |
| 47 | Legacy EPP names | GIE v0.5.1–v1.0.x: `inference_model_request_total`; v1.1–v1.5: `inference_objective_request_total`. llm-d-router v1.0 emits only `llm_d_epp_*` | documented (source) | GIE@v0.5.1/v1.1.0/v1.5.0 `pkg/epp/metrics/metrics.go`; LR `docs/metrics.md` L781-812 · 2026-10-28 |
| 48 | P/D double count | The prefill pods' `vllm:request_success_total` also counts each request (`max_tokens=1`), and decode counts it too. Summing them gives about 2× | documented | LD `guides/pd-disaggregation/README.md` L682; LR `pkg/sidecar/proxy/connector_nixlv2.go` · v1.0.0 · 2026-10-28 |
| 49 | Traffic separation | EPP `fairness_id` comes from header `x-llm-d-inference-fairness-id` (default `default-flow`); `priority` comes from `x-llm-d-inference-objective`. EPP `/metrics` is on :9090 with auth on | documented | LD `docs/api-reference/epp-http-headers.md` L11-33; LR `docs/metrics.md` L1-104 · v1.0.0 · 2026-10-28 |
| 50 | LLMInferenceService | `serving.kserve.io`, v1alpha1 from KServe v0.16, v1alpha2 storage in v0.21. It owns Deployments `<n>-kserve` and `<n>-kserve-prefill`, LWS `<n>-kserve-mn`, EPP `<n>-kserve-router-scheduler`, InferencePool `<n>-inference-pool`, HTTPRoute `<n>-kserve-route` | documented (source) | KS `config/crd/full/llmisvc/`, `pkg/controller/v1alpha2/llmisvc/controller.go` L413-463 · v0.16–0.21 · 2026-10-28 |
| 51 | LLMISVC pod labels | `app.kubernetes.io/name=<llmisvc>`, `app.kubernetes.io/part-of=llminferenceservice`, `app.kubernetes.io/component=llminferenceservice-workload[-prefill…]`, `kserve.io/component=workload`, `llm-d.ai/role=decode\|prefill\|both`. The EPP has component `llminferenceservice-router-scheduler` | documented (source) | KS `pkg/constants/constants.go` L420-469 · v0.21.0 · 2026-10-28 |
| 52 | Plain InferenceService | Pods are labelled `serving.kserve.io/inferenceservice=<isvc>`, `component=predictor`. Modes: `Standard` (formerly RawDeployment) and `Knative` (formerly Serverless) | documented (source) | KS `pkg/constants/constants.go` L61, L406, L576-580 · v0.21.0 · 2026-10-28 |
| 53 | kube-state-metrics GPUs | `kube_pod_container_resource_requests{resource,unit,pod,namespace,node}`; resource names are sanitised (`nvidia.com/gpu` → `nvidia_com_gpu`). `kube_pod_owner{owner_kind,owner_name}` | documented (source) | KSM `docs/metrics/workload/pod-metrics.md`, `internal/store/pod.go` · main · 2026-10-28 |
| 54 | `kubernetes.io/hostname` | The kubelet sets it to the host's hostname; it "can be changed … by `--hostname-override`", so it can differ from the node name | documented | `https://kubernetes.io/docs/reference/labels-annotations-taints/` (source: kubernetes/website main) · 2026-10-28 |
| 55 | `status.nodeInfo.systemUUID` | "For unique machine identification MachineID is preferred. This field is specific to Red Hat hosts." The kubelet (cAdvisor) reads `/sys/class/dmi/id/product_uuid` | documented (source) | `https://github.com/kubernetes/api/blob/abcd566/core/v1/types.go` L7154-7160; `https://github.com/google/cadvisor/blob/c57813a/lib/utils/sysfs/sysfs.go` L448-449 · 2026-10-28 |
| 56 | SMBIOS UUID byte order | Linux prints SMBIOS ≥ 2.6 UUIDs with the first 3 fields little-endian (`%pUl`) and older ones big-endian, in lowercase. All-00 or all-FF UUIDs are dropped (empty) | documented (source) | `https://github.com/torvalds/linux/blob/8a36f53/drivers/firmware/dmi_scan.c` L234-268 · 2026-10-28 |
| 57 | server-scan `system_uuid` | Raw vendor value (UCS `uuid`, Intersight `Uuid`, OneView `uuid`, Redfish `UUID`), not normalised and **not unique**. The UCS value is the *service-profile pool* UUID and can repeat | documented (source) | SS `backend/app/infrastructure/mongodb/indexes.py` L37-45, `providers/*/mapping.py` · defe172 · 2026-10-28 |
| 58 | server-scan node match | `clean_hostname(node.metadata.name)`: lowercase, strip domain → server `name`, stored as `reported_name`. Falls back to the serial over SSH. For Agents it uses `status.inventory.systemVendor.serialNumber` first | documented (source) | SS `backend/app/infrastructure/openshift/records.py` L34-56, L125-160; `application/services/openshift_membership.py` · defe172 · 2026-10-28 |
| 59 | Node → Machine → BMH | Node annotation `machine.openshift.io/machine`; Machine annotation `metal3.io/BareMetalHost`. BMH `status.hardware.systemVendor.{manufacturer,productName,serialNumber}` | documented (source) | OSG `machine-api-operator/blob/6c82e84/pkg/controller/nodelink/nodelink_controller.go` L22; `cluster-api-provider-baremetal/blob/51bc686/…/actuators/machine/actuator.go` L52; `baremetal-operator/blob/257604b/apis/metal3.io/v1alpha1/hardwaredata_types.go` L202-233 · 2026-10-28 |
| 60 | Agent (assisted/MCE hub) | `status.inventory.{hostname, systemVendor.serialNumber, gpus[]}`, `spec.clusterDeploymentName` | documented (source) | OSG `assisted-service/blob/77b43f4/api/v1beta1/agent_types.go` · 2026-10-28 |

### Decisions pending live verification

Provisional choices, each *inferred* from the documented rows above. The last column names the runbook output that confirms or refutes it.

| # | Decision | Provisional value | Verified by |
|---|---|---|---|
| D1 | `dcgm_node_label` | `Hostname` (rows 25, 28). There is no `node` label; `instance` is the exporter pod IP | R9 `10`, `11`, `13` |
| D2 | `R` per node | Count **distinct `UUID`** per `Hostname`, not series. MIG and virtual-GPU modes can add series per GPU | R9 `11` vs `12`, `16` |
| D3 | Pod labels on DCGM | `exported_pod`, `exported_namespace`, `exported_container` (row 29). Attribution still comes from the kube API (§20.3) | R9 `17`/`18`, R8 `monitors.json` |
| D4 | `allocated_idle` signal | `DCGM_FI_PROF_GR_ENGINE_ACTIVE < 0.05` (a ratio) where the GPU model reports it, else `DCGM_FI_DEV_GPU_UTIL < 5` (percent). Under MIG, use the parent-GPU series | R9 `14` vs `15`, `16` |
| D5 | Run:ai present on a cluster | Only if `runaiconfigs.run.ai` exists or namespace `runai` exists. A bare `scheduling.run.ai` group means KAI (row 16), not Run:ai. **No Run:ai** → row 16 never matches; GPUs fall to rows 17–22 (serving, `WORKLOAD_NOT_READY`, `OTHER_WORKLOAD`, `NO_WORKLOAD`) | R2, R6 |
| D6 | Pod → Run:ai workload | Walk `ownerReferences` to the top `run.ai` kind. Expected chains (inferred from rows 4, 7; **not documented**): `Pod>RunaiJob>Training\|InteractiveWorkload`, `Pod>PyTorchJob\|MPIJob>DistributedWorkload`, `Pod>RS>Deployment>Revision>Configuration>Service>InferenceWorkload`, `Pod>…LWS>DistributedInferenceWorkload` | R5 `chain-shapes.tsv` |
| D7 | Fractional GPUs | The GPU holder is the reservation pod. Credit it to the fractional pods with the same `runai-gpu-group` on that node, and classify by *their* workload. Reservation pods are never `OTHER_WORKLOAD` | R6 `fractional-pods.tsv`, `reservation-pods.json` |
| D8 | Counter of record | Plain vLLM and ISVC-vLLM: Σ over replicas of `vllm:request_success_total` (all `finished_reason`; keep the per-reason split in evidence). LLMISVC/llm-d: the EPP counter (`llm_d_epp_request_total`, or the legacy name from row 47). Never add prefill and decode vLLM counters (row 48) | R9 `30`–`33`, R7 |
| D9 | Synthetic/health traffic | Probes don't count (row 44). Synthetic completions are indistinguishable unless sent through the EPP with a dedicated `fairness_id` → "traffic observed, user attribution unverified" | R9 `33` (`fairness_id` values) |
| D10 | Node join | `clean_hostname(node)` = `clean_hostname(reported_name or name)` (server-scan's own rule, row 58). Corroborate with the BMH serial through Node→Machine→BMH where present. `systemUUID` is weak evidence only: case-fold, flag first-3-field byte swaps, and UCS UUIDs repeat | R3 `nodes.tsv`, R11 `joincheck.json` |
| D11 | Thanos access | Route host from `openshift-monitoring/thanos-querier`, `/api` only. ServiceAccount bound to `cluster-monitoring-view`; the engine batches at most a few queries per 30 s | R0 `00-auth` |
| D12 | `demand_window` 24 h | Needs user-workload retention > 24 h, else the window is truncated (row 37) | R9 `01-retention-days` |

### Lab cluster observations (2026-10-31)

`lab-tested` on a Red Hat workshop cluster (OpenShift 4.22.13, Kubernetes v1.35.6, 3 nodes, **no GPUs**, no Run:ai, user-workload monitoring on). Read-only: `oc get`, `oc get --raw`, one short `oc port-forward` to read the EPP's `/metrics`. Workshop data, not production.

| # | Observation | Bears on |
| --- | --- | --- |
| L1 | `serving.kserve.io/v1alpha2` LLMInferenceService `qwen-tiny` owns Deployments `qwen-tiny-kserve` (vLLM, `llm-d.ai/role=both`) and `qwen-tiny-kserve-router-scheduler` (EPP), InferencePool `qwen-tiny-inference-pool` (both `inference.networking.k8s.io/v1` and `x-k8s.io/v1alpha2` CRDs present). Chain: Pod → ReplicaSet → Deployment → LLMInferenceService (controller refs). Pod labels match fact 51 (`app.kubernetes.io/component=llminferenceservice-workload` / `…-router-scheduler`, `kserve.io/component=workload`). | Facts 50–51 confirmed; D6 (serving side); `holder_ref` = the LLMInferenceService |
| L2 | vLLM image `vllm/vllm-openai-cpu:v0.32.0`; live `/metrics` (port 8000, plain HTTP) has `vllm:request_success_total{engine,finished_reason,model_name}` with the five `finished_reason` values pre-created at 0, plus `num_requests_running/waiting`, `prompt/generation_tokens_total`, `kv_cache_usage_perc`. | Facts 41–42 confirmed for v0.32.0 |
| L3 | EPP image `llm-d-router-endpoint-picker:v0.10.0` (older than the v1.0.0 that fact 46 documents). Its `/metrics` (port 9090) needs a bearer token (401 without; fact 49). It exports **both** `llm_d_epp_request_total{fairness_id,model_name,priority,target_model_name}` **and** the legacy `inference_objective_request_total{model_name,priority,target_model_name}`, equal (5 and 5) for the same requests. | D8: candidates are tried in order and **never summed** (summing would double count); prefer `llm_d_epp_request_total`, which also carries `fairness_id` for D9 |
| L4 | `fairness_id` values are set by clients (one seen: an e2e test team id), so synthetic traffic can be excluded by a matcher on it once the user names the values (Q6). | D9 |
| L5 | No ServiceMonitor/PodMonitor exists for the model server or EPP, so Thanos has **no** `vllm:*`, `llm_d_*` or `inference_*` series. Upstream KServe does not create one (fact 40's monitor comes from RHOAI's odh-model-controller). | Serving traffic needs a monitor per cluster; without one the engine reports `TRAFFIC_UNOBSERVED` (UNKNOWN), never `NO_REAL_DEMAND`. Ask the platform owners whether production has monitors (R8 `monitors.json`) |
| L6 | 93 of 95 pods in the namespace are finished or `ContainerStatusUnknown` from earlier rollouts; the pod list needs a phase filter. | Kube adapter lists with `status.phase!=Succeeded,status.phase!=Failed` |
| L7 | The API certificate is signed by the cluster's own `kube-apiserver-lb-signer`; the `*.apps` route (Thanos) has a public CA. | Separate `ca_bundle` and `thanos_ca_bundle` per cluster; TLS verification never disabled |
| L8 | The Thanos querier route (via kube-rbac-proxy) accepts `partial_response=false` (HTTP 200); 8 instant queries take 1.5–2.3 s in total. A bad token gets HTTP 401. | Facts 34–36: the Thanos source sends `partial_response=false` and fails the whole read on error, so a partial answer never reads as zero traffic |
| L9 | Thanos returns PromQL **info** annotations in `warnings` with `status: success`, e.g. `PromQL info: metric might not be a counter, name does not end in _total/…` on `increase()`. | Warnings starting with `PromQL info:` are logged, not failures; any other warning fails the read |
| L10 | `increase()` returns no series for a counter with fewer than two samples in the window, so a serving pod scraped for under ~2 scrape intervals has no value yet. | Such a deployment is `TRAFFIC_UNOBSERVED` (UNKNOWN) for its first minutes, never zero |
| L11 | Owner chains on the cluster include **cluster-scoped** owners (an Argo CD instance owned by `GitopsService cluster`, `pipelines.openshift.io`; static pods owned by their `Node`). | The owner walk follows cluster-scoped owners (object GETs only; Secrets and OAuth token objects refused) |
| L12 | **Engine end to end** (`ttv-engine --once`, ledger writes on in a lab database): server-scan sandbox + this cluster standing in for sandbox cluster `upi-tlv`, `cluster_id` pinned. Kube read 2.7 s (3 nodes, 0 GPU pods), Thanos read 2.3 s (8 queries), whole scan 3.6 s with the detail cache warm; a second scan changed nothing. `/explain` shows rows 11–12 with evidence. | Plumbing `lab-tested`: auth, TLS, deadlines, grace, node lookup, conflicts, ledger columns `cluster`/`node_uid` |
| L13 | The sandbox's `upi-tlv` has 76 names shared by several INSTALLED records (up to four vendors per name, e.g. `ocp4-prep-tlv-compute-01`), none with `openshift_reported_name` or `contested_with`. Of 2,401 INSTALLED sandbox records, 54 have a reported name and 24 are contested. | Seeded demo data; real server-scan falls back to the serial over SSH when a hostname is ambiguous (fact 58). The engine raises a `NODE_CLAIM` conflict and reports those GPUs UNKNOWN (`IDENTITY_CONFLICT`) rather than guessing: 266 GPUs here, plus 102 `NODE_NOT_FOUND` because the workshop nodes are not sandbox servers |
| L14 | **GPU rows end to end on simulated GPUs** (`deploy/lab/`): a worker advertising 8 fake `nvidia.com/gpu`, a fake DCGM exporter reporting 7, and GPU-holding pods, matched to one synthetic server-scan record. The engine gave, with the documented label shapes (`Hostname`, `UUID`, `exported_pod` after the scrape renames `pod`): 1 `GPU_MISSING` (POPAI, evidence A=8 R=7 and XID 79), 2 `SERVING_IN_USE` (productive, team from `team_map`, flagged `low_use_unverified` and `user_attribution_unverified`), 1 `NO_REAL_DEMAND`, 1 `TRAFFIC_UNOBSERVED` (no scrape), 1 `WORKLOAD_NOT_READY` (crash loop past `warmup_grace`), 1 `OTHER_WORKLOAD`, 1 `NO_WORKLOAD`. Before the fake GPUs existed the same node was `GPU_PLATFORM_NOT_READY` (POPAI), and the POPAI episode carried into `GPU_MISSING`. A new scrape read `TRAFFIC_UNOBSERVED` for one scan, then the right row (L10). | Rows 14–22 `lab-tested` with simulated GPUs; real GPUs, the DCGM exporter and Run:ai remain unverified |
| L15 | The EPP's `/metrics` accepts a service-account token whose ClusterRole allows `GET` on the non-resource URL `/metrics`; user-workload Prometheus scraped it through a ServiceMonitor with `authorization.credentials`. The Envoy AI Gateway route rejects unauthenticated requests (`Jwt is missing`). | How production EPP metrics can be scraped. Synthetic checks through the gateway (§7, off by default) would need an authorised client identity |

### Read-only probe runbook

In `docs/discovery-probes.md`, Part C (R0–R12).

## I2 calibration (useful-use threshold)

Status: **pending production data.** I2 needs at least 7 days of genuine-request distributions per serving deployment (counter of record per D8).

- Probe R10 (Part C) pulls hourly `increase()` per deployment over the retention Thanos actually holds (R9 `01-retention-days`).
- `lab-tested` 2026-10-30 (sandbox cluster, OpenShift 4.20.35, R9 `01-retention-days`): `prometheus-user-workload` held 1.05 days, `prometheus-k8s` 15.6 days. That matches the 24 h default, so expect the fallback below unless production differs.
- If user-workload retention is under 7 days (the default is 24 h), run R10 once a day for 7 days, or ask the platform owners to raise retention (RHOAI recommends 15 d). The sanitised `i2/` folders come back as files and are analysed here.
- The proposal to the user: per model class, the distribution of genuine requests per GPU-hour (p10/p50/p90), and a threshold for `LOW_USE`. Until approved, `LOW_USE` stays "unverified" and only `NO_REAL_DEMAND` (zero genuine requests) applies (§0.7 I2).
- Assumption (A1, reversible): `abort` and `error` finished requests count as demand; the success split (`stop`/`length`/`repetition`) is kept in evidence.

## AWS pricing (slice 4, ADR-0003)

Status: **built; snapshot captured 2026-10-31.** Region us-east-1 (I3, user decision 2026-10-31). Proxies and the size rule need the user's approval (ADR-0003, "Needs user approval").

### Price list format

`documented` (read on the real file): `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.csv`, retrieved 2026-10-31. Size 302,903,252 bytes, sha256 `cce748e4a6eaa35e349e2590ef36a957071b1e01b316369aaccaf51d270abe33`, 393,699 data rows.

- Five metadata lines (`FormatVersion` v1.0, `Disclaimer`, `Publication Date` **2026-10-29T17:56:34Z**, `Version` 20261029175634, `OfferCode` AmazonEC2), then a 93-column header starting with `SKU`. Every field is quoted.
- Column names are as in ADR-0003's filter list. GPU counts are in `GPU`. `GPU Memory` is inconsistent (`640 GB HBM3` total for p5, `141 GB HBM3e` per GPU for p5en), so it is kept verbatim as evidence only.
- **Capacity Blocks carry `TermType=OnDemand` at $0.00** (`MarketOption=CapacityBlock`, `operation=RunInstances:CB`) for p4d, p5, p5en and p6. Without the `MarketOption=OnDemand` filter these rows would price GPUs at zero, and the duplicate check catches them.
- `g4dn.metal` is `Product Family=Compute Instance (bare metal)`, not `Compute Instance`.
- `p5e.48xlarge` (8 x H200) has **no** row of any kind in the us-east-1 file; H200 maps to `p5en.48xlarge`.
- Every priced row's `EffectiveDate` is 2026-10-01, before the publication date.

### Mapping and snapshot `aws-ec2-ondemand-us-east-1-20261029T175634Z-map-2026-10-31.2`

Instance facts are `documented` from AWS's instance-type specifications, retrieved 2026-10-31: [ac.html](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html) (current generation) and [pg.html](https://docs.aws.amazon.com/ec2/latest/instancetypes/pg.html) (previous generation, p3dn only). Prices are `documented` from the price list above. The capture cross-checked every GPU count against the price list's `GPU` column.

| Canonical model | Instance | Kind | GPUs | Instance $/h | $/GPU-h | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| NVIDIA H100 80GB | p5.48xlarge | exact | 8 | 55.04 | 6.88 | AWS uses SXM; PCIe H100 is valued at the SXM rate |
| NVIDIA H200 141GB | p5en.48xlarge | exact | 8 | 63.296 | 7.912 | p5e has no us-east-1 offer |
| NVIDIA B200 180GB | p6-b200.48xlarge | exact | 8 | 113.9328 | 14.2416 | AWS states 179 GiB per GPU |
| NVIDIA B300 288GB | p6-b300.48xlarge | exact | 8 | 142.416 | 17.802 | AWS: 8 x 268 GiB; added 2026-10-31 |
| NVIDIA A100 40GB | p4d.24xlarge | exact | 8 | 21.957642 | 2.74470525 | SXM4 |
| NVIDIA A100 80GB | p4de.24xlarge | exact | 8 | 27.44705 | 3.43088125 | SXM4 |
| NVIDIA L40S 48GB | g6e.12xlarge | exact | 4 | 10.49264 | 2.62316 | g6e.48xlarge: 3.7663975 |
| NVIDIA L4 24GB | g6.12xlarge | exact | 4 | 4.6016 | 1.1504 | g6.48xlarge: 1.6688 |
| NVIDIA T4 16GB | g4dn.12xlarge | exact | 4 | 3.912 | 0.978 | g4dn.metal: 0.978 |
| NVIDIA RTX PRO 6000 Blackwell 96GB | g7e.24xlarge | exact | 4 | 16.57216 | 4.14304 | g7e.48xlarge: 4.14304 |
| NVIDIA Tesla V100 32GB | p3dn.24xlarge | exact | 8 | 31.212 | 3.9015 | previous generation, SXM2 |
| NVIDIA A10 24GB | g5.12xlarge | **proxy** | 4 | 5.672 | 1.418 | A10G; g5.48xlarge: 2.036 |
| NVIDIA A100 (memory unknown) | p4d.24xlarge | **proxy** | 8 | 21.957642 | 2.74470525 | lower bound (40GB rate) |
| NVIDIA H100 NVL 94GB | p5.48xlarge | **proxy** | 8 | 55.04 | 6.88 | no H100 NVL on EC2; nearest H100 |
| NVIDIA H100 94GB | p5.48xlarge | **proxy** | 8 | 55.04 | 6.88 | inventory "H100 95GB"; nearest H100 |
| NVIDIA H100 (memory unknown) | p5.48xlarge | **proxy** | 8 | 55.04 | 6.88 | every H100 size has this rate |

All proxies were approved by the user on 2026-10-31.

Unpriced, with the reason recorded in the snapshot: every AMD Instinct model (EC2 has none), A16, A30, A40, A2, L40 (G6e is the L40S), A800, H800, Quadro RTX 6000/8000, Tesla P100, Tesla V100 16GB (p3 is no longer in AWS's specification pages, although the price list still prices it at $3.06/GPU-h), and the `(memory unknown)` families of P100, V100 and A800. Raw models outside the catalogue (e.g. `NVIDIA RTX A6000`) have no canonical name and are unpriced.

Snapshot file: `config/pricing/snapshots/aws-ec2-ondemand-us-east-1-20261029T175634Z-map-2026-10-31.2.json`, content sha256 `e51c649cbfa1e73197cfc3c7e4e5d8037c6f48740253b6fbb9be5e042eebf901`. Parsed with `ttv-pricing capture --region us-east-1 --from-file` from the same raw download as map .1 (raw sha256 unchanged), which it supersedes. Map .1 was never used outside the lab.

### Raw GPU model names

`lab-tested` on the lab ledger (`SELECT DISTINCT gpu_model FROM capacity_intervals`, sandbox server-scan data): `NVIDIA H100 80GB`, `NVIDIA A100 80GB`, `NVIDIA A100 40GB`, `NVIDIA L40S 48GB`, `NVIDIA T4 16GB`, `NVIDIA A16 16GB`, `AMD Instinct MI300X 192GB` (already canonical), `UCSC-GPU-A100-40` → `NVIDIA A100 40GB` (local PID rule), `NVIDIA A100` → `NVIDIA A100 (memory unknown)`, and `NVIDIA RTX A6000` → none. The last three are the cards server-scan's fake generator deliberately leaves uncatalogued (`docs/fake-data.md@defe172:93-95`). Whether `UCSC-GPU-A100-40` is a real Cisco PID is **unverified**; production probe P-SS output will show the real strings.

### Lab result (`lab-tested`, not production evidence)

`ttv_lab_pricing`, 2026-10-30 21:56 → 23:50 UTC (about 1.9 h of scans of the sandbox), after `ttv-pricing import` and the engine's startup alias refresh:

| Stage owner | Idle GPU-h | Idle USD | Unpriced idle GPU-h | Overdue GPU-h | Overdue USD |
| --- | --- | --- | --- | --- | --- |
| DC | 1,095.75 | 2,236.71 | 305.44 | 0 | 0 |
| Redbull | 408.52 | 1,258.42 | 34.36 | 408.52 | 1,258.42 |
| Serving | 2.85 | 19.62 | 0 | 0 | 0 |
| POPAI | 1.78 | 12.27 | 0 | 0 | 0 |

Historical equals present-day here: the only snapshot was published before the first interval. Idle now: **$1,865.30 per hour** for 615 priced idle GPUs; **178 idle GPUs unpriced** (104 MI300X, 58 RTX A6000, 16 A16).

## Contradictions with AGENT.md Section 0

Reported per §20.10 ("stop and report any contradiction with Section 0"). Resolutions are the user's, dated.

| # | Contradiction | Status |
| --- | --- | --- |
| C1 | §0.1 gives DC "assigned but not applied, or failed to apply", and §0.3 assumes presence in server-scan means a qualifying profile. server-scan qualifies a server on the name of an **assigned** profile only: Cisco joins on `assigned_to_dn` (`backend/app/infrastructure/providers/ucs_manager/mapping.py@defe172:732`), Dell keeps any profile with a target and never reads `ProfileState` (`openmanage/provider.py@defe172:365-388`). So an `ocp*` server whose profile is not applied or failed is in server-scan and §20.2 row 8 charges Redbull. | **Resolved 2026-10-28: server-scan presence wins** (Redbull). DC's not-applied/failed codes cover only servers server-scan cannot see. Known limitation: Redbull's queue can include profile-application time. |
| C2 | §0.3 "every collector applies a profile-name filter" holds only by configuration: the code default pattern is `""` (collect everything); the shipped Helm value is `^ocp`; Intersight falls back to the server's UserLabel; REDFISH_STANDALONE is exempt by design (consistent with the DGX case). | **Open:** the production pattern is verified by probe P-SS-1. The platform's `ocp*` rule copies server-scan's: case-sensitive `re.search("^ocp", <profile name>)`. |
| C3 | §0.2 (Run:ai work is productive by presence) vs §20.2 row 6 / I8: `health.overall == CRITICAL` includes non-GPU policies (`raid.os_mirror_missing`, `storage.name_capacity_mismatch`, `network.single_link_up`, all CRITICAL in `domain/services/health/health_policy_defaults.py@defe172`), and row 6 runs before the GPU rows, so a busy node would be IDLE/DC. | **Resolved 2026-10-28: running work wins.** For a server that is a Ready node in a target cluster, `HARDWARE_CRITICAL` and `BMC_UNREACHABLE` become flags and the GPU rows decide. Row 6 still applies to `MAINTENANCE`, and to servers not yet Ready in a cluster. §20.2 is amended accordingly before code. |
| C4 | §4 "use the same read-only manager accounts server-scan uses": server-scan's iDRAC account needs Configure Manager for iDRAC reset (ADR-0050), and its UCS/OME roles are not documented as read-only. | **Resolved 2026-10-28: reuse server-scan's accounts.** Read-only is enforced by the collector's code (GET/query only). |
| C5 | §0.3 "server-scan collects from UCS Manager/Central": there is no standalone UCS Manager collector; a domain not registered with UCS Central is invisible to server-scan (ADR-0014). | **Open:** question Q2. Consistent with the user's manager scope if every domain is registered. |
| C6 | §0.2 lists "runaijobs" as productive but a notebook as idle. A RunaiJob with `priorityClassName: build` is interactive, and workspaces (`InteractiveWorkload`) very likely run through a RunaiJob (inferred; R5 checks the chain). | **Resolved 2026-10-28: interactive is idle.** Classify by the top `run.ai` owner kind. |
| C7 | §0.2 "deployment": Run:ai has no native kind of that name. A vLLM Deployment scheduled by `runai-scheduler` in a Run:ai project matches both row 16 (presence) and row 17 (genuine traffic). | **Resolved 2026-10-28: genuine traffic.** Presence applies only to native `run.ai` kinds. |

Not Section 0, but §20.4 needs correcting before code:
- server-scan's id field is `id`, not `_id`.
- No list endpoint returns every field; only `GET /api/v1/servers/{id}` does (see server-scan §1 for the proposed `rows` + detail sync).
- Audit events are kept 180 days only once `auditRetention.reportOnly` is false; the default deletes nothing.
- `OPENSHIFT_STATE_CHANGED` fires only when `lifecycle_state` changes, so cluster-to-cluster moves emit no event.
- server-scan's own `docs/cisco-collectors.md@defe172:736-739` says `ucscsdk` has no graphics class; 0.9.0.8 ships `GraphicsCard`.
- §20.3: `R` is the count of **distinct GPU `UUID`s** per `Hostname`, not of `DCGM_FI_DEV_POWER_USAGE` series (MIG and vGPU add series) (D2).
- §20.2 row 15: the default DCGM field set has no ECC counters, and the platform may not change ClusterPolicy (§0.4). `GPU_MISSING` evidence is the last XID plus remapped-row fields unless POPAI enables ECC fields.
- §20.2 `allocated_idle`: `DCGM_FI_PROF_GR_ENGINE_ACTIVE` is a 0–1 ratio and `DCGM_FI_DEV_GPU_UTIL` is 0–100, so the 5% threshold is unit-aware (D4).
- §20.8: the ClusterRole widening above (approved).
- §20.10 / row 19: user-workload Prometheus keeps 24 h by default, so Thanos may hold neither the 7-day I2 history nor a full 24 h `demand_window` (D12). See I2 below.

## Open questions for the user

Answerable now (no probe needed):
- **Q1** Is the REDFISH_STANDALONE collector enabled in production, and does it list every DGX? Is `standalone` acceptable as the vendor part of a DGX asset id (`standalone:<serial>`)?
- **Q2** Is any UCS Manager domain *not* registered in UCS Central? (Such a domain is invisible to server-scan and to the collector.)
- **Q3** Is production server-scan's auth enabled? Who issues the AUDITOR token, and is `auditRetention.reportOnly` false?
- **Q4** Is there a non-seeded staging server-scan with servers in INSTALLED_TO_INVENTORY and real `OPENSHIFT_STATE_CHANGED` events? (The sandbox has neither.)
- **Q5** Intersight is out of scope: confirm no profile-less GPU servers live there.
- **Q6** Does user traffic reach models through the llm-d inference gateway (EPP) or straight to Services/Routes? Do canaries or health checkers send real completions, and could they set a dedicated `x-llm-d-inference-fairness-id` so they can be excluded (D9)?
- **Q7** Will the platform owners raise user-workload Prometheus retention to at least 7 days (ideally 15)? If not, daily R10 snapshots are the I2 fallback.
- **Q8** `GPU_MISSING` evidence: accept XID plus remapped rows (the working assumption), or ask POPAI to enable the DCGM ECC fields?

Answered by the probe runbook (pending):
- Production name patterns (`INVENTORY_COLLECTOR_NAME_PATTERN` and per-type overrides) and whether any profile names start with upper-case `OCP`.
- Deployed versions: UCS Central, UCS Manager per domain, OME (3.x vs 4.x), iDRAC generations; any MX / OME-Modular.
- Exact `cluster_name` strings server-scan reports for `ocp4-prod-gpu-site*`.
- Manager validation gates: UCS Central `graphicsCard` population, `lsSPMeta` localized count, OME GPU inventory type, OME `ProfileState` values, the UCS read-only role resolving `lsServer`/`lsBinding`/`lsServerFsm`.
- Per cluster: Run:ai presence (D5) and versions (Run:ai, GPU Operator, RHOAI/KServe, llm-d); the DCGM node label and pod labels (D1, D3); profiling-metric availability (D4); pod → workload chain shapes (D6); fractional-GPU reservation pods (D7); serving counters present (D8); node-name join against server-scan (D10); node labels that mark the Run:ai pool versus serving nodes.
