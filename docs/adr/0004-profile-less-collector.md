# ADR-0004: Profile-less GPU collector

- **Status:** accepted 2026-10-31. Built and **`fixture-tested` only**: there is no UCS Central, OpenManage or iDRAC in the lab, so both readers run against synthetic captures. The production probe (`docs/discovery-probes.md` Part B) is the first real evidence, and each reader lists the validation gates it must pass.
- **Context:** user decision of 2026-10-30, "Simplified profile-less collector" (`docs/discovery.md`, Inputs); `docs/discovery.md` "Managers and the profile-less collector"; AGENT.md §4 (connector behaviour); ADR-0002 rows 2-5.
- **Code:** `src/ttv/collector/` (`main.py` the job, `ucs_central.py`, `openmanage.py`, `redfish_gpu.py`, `blocking.py`); `src/ttv/classify.py` (`_CollectorRules`); `src/ttv/engine.py` (`_collector_assets`, `_collector_status`); `src/ttv/store.py` (`record_collector_read`, `load_collector_state`); `migrations/0003_collector.sql`, `migrations/0004_collector_profile_unresolved.sql`. Tests: `tests/test_collector_*.py`, `tests/test_classify_collector.py`, `tests/test_engine_collector_pg.py`.

## Decision

server-scan only lists servers it reaches through an OCP service profile, so a GPU server with no profile, or with a profile for something else, is invisible to it. The collector reads every server UCS Central and OpenManage know, read-only, and the engine classifies the GPU servers that server-scan does not list:

| Collector sees | ADR-0002 row | Result |
| --- | --- | --- |
| Manager stale, or no status | 2 `SOURCE_STALE` | UNKNOWN |
| Same serial twice, or profile binding unresolved | 2b `IDENTITY_CONFLICT` | UNKNOWN |
| No profile bound | 3 `PROFILE_NONE` | IDLE, owner DC, clock from the collector's first sighting (a lower bound) |
| A profile not matching `ocp_profile_pattern` (`^ocp`, `re.search`) | 4 `OTHER_INTENDED_USE` | NOT_CHARGED (shown, never charged) |
| An OCP profile, but not in server-scan | 5 `HANDOVER_PENDING` | UNKNOWN; flag `handover_overdue` once the profile has been seen for longer than `handover_grace` (6 h) |

- **server-scan wins.** A record whose `vendor:serial_normalized` server-scan also lists is ignored; the server-scan rows apply (an AVAILABLE GPU server there is Redbull's).
- **No server-scan listing, no collector-only rows.** "Not in server-scan" needs at least one server-scan listing since the engine started. A failed cold-start scan holds every collector server at its last ledger state and counts it as `collector_held_no_serverscan`; otherwise one scan would reclassify server-scan's own servers (for example to `HANDOVER_PENDING`). After that the last good listing is used, as for server-scan's own rows.
- **Identity** is `vendor:serial_normalized` with server-scan's normalisation. A record with no usable serial gets `vendor:collector-id:<manager>:<source_id>` and is never merged. The same serial from two records is a `COLLECTOR_DUPLICATE` conflict.
- **Only servers with GPUs are classified.** `gpu_models=()` means the manager reports none; `None` means GPU inventory was not read, and the engine keeps the server's last state instead of reading it as zero.

### Completeness

`CollectorRead.complete` means **the server and profile listings were read in full**, so a server missing from the read is absent. Gaps for one server stay on that server: `gpu_models=None` when its GPUs could not be read, and `profile_unresolved` when its profile binding is ambiguous.

The engine uses each manager's **latest complete read**. A manager whose latest complete read is older than `TTV_COLLECTOR_STALE_AFTER` (1 h) turns its servers UNKNOWN. A server is retired only when it has been missing from complete reads for `tombstone_after`, and only in a scan where every manager's latest attempt was complete. An incomplete or failed read never retires anything.

Both readers first marked the whole read incomplete for a single server's gap (one failed iDRAC, one failed UCS Manager domain, one ambiguous name). Because the engine ignores incomplete reads, one bad BMC would have hidden a whole OME estate and, an hour later, turned it all UNKNOWN. That was changed at merge time (2026-10-31) to the rule above.

### The job (`ttv-collector`)

- **A separate process** from the engine. Every `TTV_COLLECTOR_INTERVAL_SECONDS` (900) it reads all managers concurrently and stores each read with its servers in one transaction. It applies migrations at start, and a Postgres advisory lock (`COLLECTOR_LOCK_KEY`, separate from the engine's) keeps one collector active.
- **It stores reads whatever `TTV_DRY_RUN` says.** Reads are raw observations in our own Postgres, not ledger writes, and the engine's dry run needs them. The job never writes to a manager; both readers enforce that in code (below).
- **A read that raises** is stored as a failed attempt (`complete=false`, no servers, the error), so it blocks retirement and, after an hour, shows as staleness. A crash in a reader (`UnsafeRequestError` included) is logged and recorded the same way; it never stops the loop.
- **No outer deadline.** Each reader bounds itself: OME by a whole-read deadline, UCS by a deadline on every SDK call. Cancelling a UCS read from outside could send a logout while an abandoned SDK thread still holds the handle, which the per-call poisoning exists to prevent. A read that hangs anyway shows up as a stale manager.
- **Probe mode:** `ttv-collector --once --dry-run --servers` reads every manager, prints each record, stores nothing and needs no database. `--print-config` prints the settings (database password masked) and the managers file.

### Removing a manager

Each start of the job records the managers in `TTV_MANAGERS_FILE` in `collector_managers` and sets `removed_at` on every other manager it has read. The engine, Evidence and the "collector manager stale" alert then ignore a removed manager's reads. Its servers are treated like servers missing from a complete read: they keep their last state and retire after `tombstone_after`, unless another manager or server-scan lists them. Adding the manager back clears `removed_at`. Before this, a decommissioned manager stayed stale forever, which kept its servers UNKNOWN, blocked every retirement and kept the alert firing.

## Cisco (UCS Central)

- **Status:** implemented 2026-10-31. **`fixture-tested` only:** there is no UCS Central in the lab, so the real ucscsdk/ucsmsdk handles run against synthetic XML (`tests/fixtures/ucs_central/`).
- **Context:** user decision of 2026-10-30 ("Simplified profile-less collector", `docs/discovery.md` Inputs); `docs/discovery.md` "Managers and the profile-less collector" (UCS Central, Identity join, Reuse recommendation).
- **Code:** `src/ttv/collector/ucs_central.py` (`UcsCentralSource`), `src/ttv/collector/blocking.py`; tests `tests/test_collector_ucs_central.py`.

### What one read does

1. **One UCS Central session** (ucscsdk 0.9.0.8, server-scan's pin): five whole-class queries, `computeSystem`, `computeBlade`, `computeRackUnit`, `lsServer`, `graphicsCard`, joined client-side by DN. Logout runs in `finally`.
2. **Equipped servers only** (server-scan's `is_equipped`). One `ManagerServer` per equipped server, with or without GPUs.
3. **GPUs from Central's `graphicsCard` replica**, grouped under their server by DN (`group_by_owning_server_dn`): one entry per equipped card, `model` verbatim, as server-scan's `_gpus`. Canonical names are resolved downstream by the pricing catalogue.
4. **UCS Manager fallback.** A registered domain with equipped servers and **zero** `graphicsCard` rows in Central is treated as "replica not populated". If `ucsm_username` is set, that domain's `computeBlade`, `computeRackUnit` and `graphicsCard` are read in one read-only UCS Manager session (ucsmsdk 0.9.18) at `computeSystem.address` (else its name), at most 4 domains at once. UCS Manager DNs are re-rooted into Central DNs (`central_external_id`). A server takes those GPUs only if UCS Manager lists **the same DN with the same normalised serial**; otherwise (e.g. a blade swapped since Central's last refresh) its GPUs stay unread instead of moving to another asset. Without the fallback, those servers carry `gpu_models=None`, never `()`.

### Bound profile (the only profile fact R1 uses)

A profile is bound, in any state, when any of these names one. The first wins:

1. `assignedToDn` on the compute row. This is server-scan's only join; server-scan's live Central run resolved 3265 of 3346 servers through it on 2026-08-18.
2. Central's `lsDn`.
3. An `lsServer` that is not a template and whose `pnDn` is this server. A domain-local `sys/...` value is re-rooted through `lsServer.domain`, matched by name, then address, then id, as server-scan does.

Other DNs named by the other joins are kept as `profile_other_dns`. The name is `lsServer.name`. If Central does not list that profile, the name comes from the `ls-<name>` RN of its DN (`profile_name_source=dn`), so a bound profile never reads as "no profile". `assignState`, `assocState`, `configState` and the compute row's `association` are evidence only.

### The record

- `vendor`: `cisco`.
- `serial_normalized`: `identity.normalize_serial(serial)`. A placeholder serial becomes `""` and the record is kept.
- `source_id`: the Central compute DN.
- `name`: the compute row's `name`, else its DN.
- `domain`: the domain name, else its id.
- `evidence`: `domain_id`, `domain_last_refreshed_ts` (raw), `domain_inventory_status`, `association`, `profile_dn`, `profile_join`, `profile_name_source`, the profile states, `gpu_source` (`central` / `ucsm` / null) and `gpu_unread_reason`.

### Failures

- **Central fails** (password file, login, any query, deadline): the read raises `SourceError`.
  - A rejected login is PERMISSION_DENIED.
  - Network failures and deadlines are TEMPORARILY_UNAVAILABLE.
  - TLS failures and unparseable replies are INVALID.
  - Central listing **no domain** is INVALID, never an empty estate.
- **A fallback domain fails** (login, deadline, no address, password file): the read stays `complete=True`, because the server listing comes from Central and is whole. It carries one error for that domain, and that domain's servers keep `gpu_models=None` with the reason. Other domains are still read.
- **No fallback configured** is not an error: `complete=True`, and `coverage.domains_gpu_unread` / `servers_gpu_unread` count what is missing.

### Read-only, enforced in code

1. `ReadOnlyHandle` exposes only `login`, `logout`, `query_classid` and `query_dn`. Any other attribute (`add_mo`, `set_mo`, `remove_mo`, `commit`, `process_xml_elem`, `post_elem`, `file_upload`, …) raises `UnsafeRequestError`.
2. Below it, the SDK handle's `post_elem` refuses any XML method except `aaaLogin`, `aaaLogout`, `configResolveClass` and `configResolveDn`. Every SDK request goes through `post_elem`, including `commit`'s `configConfMos`.

### Sessions and deadlines (copied from server-scan)

- Each SDK call runs on a daemon thread (`run_abandonable`) under `collector_timeout_seconds`.
- A call that misses its deadline poisons its session. Later calls on it, logout included, are refused, because the abandoned thread may still be using the handle. That session can stay open until the manager expires it (server-scan `docs/cisco-collectors.md@defe172`, "Timeouts, abandoned threads and poisoned clients").
- Login happens inside the `try` and logout in `finally`.
- The password is re-read with `read_secret` on every read and is never logged.

### TLS verification (differs from server-scan)

Neither pinned SDK verifies TLS:

- Both wrap sockets in `ssl.SSLContext(PROTOCOL_SSLv23)` with no certificate or host-name check.
- Both retry any SSL error as TLSv1.
- server-scan uses them as shipped.

This reader replaces each driver's handlers with `HTTPSHandler(ssl.create_default_context(cafile=ca_bundle))`. That checks the certificate chain and host name, against the system trust store when `ca_bundle` is unset. It also disables the TLSv1 retry and refuses HTTP redirects. If the pinned SDK's private layout ever changes, building the handle fails instead of running unverified.

### Copied from server-scan `defe172`

Each block carries a provenance comment.

| server-scan path | Copied |
| --- | --- |
| `infrastructure/blocking.py` | `run_abandonable` |
| `providers/ucs_common.py` | `is_equipped`, `group_by_owning_server_dn`, `TEMPLATE_TYPES` |
| `providers/ucs_central/provider.py` | `domain_id_from_dn`, `central_external_id`, the domain-address rule and the `lsServer.domain` lookup order |
| `providers/ucs_manager/client.py` | `_validate_endpoint` (adapted) |
| `ucs_central/client.py`, `ucs_manager/client.py` | the deadline and poisoning wrapper, merged into `_Session` |
| `ucs_manager/mapping.py` | the `_gpus` rule |

### Validation gates for the production probe (`docs/discovery-probes.md` Part B)

1. **`graphicsCard` replica.** Does Central populate it? Compare Central's `graphicsCard` rows per domain with the GPUs B1 reads from each UCS Manager. If Central does not, the fallback logs in to every domain that has servers on each read: 152 domains in server-scan's fleet. The fallback would then need a slow cadence.
2. **Partial replica.** A domain whose replica is only partly populated reads as complete. The per-server comparison in gate 1 catches it.
3. **Profile join.** The format of Central's `lsDn` and `lsServer.pnDn` (Central-rooted or domain-local). Also whether every `assignedToDn` resolves to a listed `lsServer`: `coverage.servers_profile_name_from_dn` above 0 means one did not.
4. **TLS.** Central's certificate, and each domain's, must chain to `ca_bundle` and match the name used. For UCS Manager that name is `computeSystem.address`, usually an IP, so the certificate needs an IP SAN. server-scan never verified these certificates.
5. **Domain freshness.** The format of `lastRefreshedTs`, and any `inventoryStatus` other than `ok`.
6. **Account role.** Whether the reused account can read `computeBlade`, `computeRackUnit` and `lsServer` on Central (their MoMeta lists write privileges only) as well as `graphicsCard`.
7. **UCS Manager account and sessions.** Whether the UCS Manager account works on every fallback domain, and the per-user session cap.

## Dell (OpenManage Enterprise)

- **Status:** built 2026-10-31, `fixture-tested` only (synthetic OME/iDRAC JSON shaped like the OME API guide, Dell's Ansible samples and server-scan's fixtures). Not yet run against a real OpenManage or iDRAC.
- **Context:** user decision of 2026-10-30, "Simplified profile-less collector" (`docs/discovery.md`, Inputs); `docs/discovery.md` "Dell OpenManage Enterprise", "Identity join", "Reuse recommendation"; ADR-0002 rows 3-5.
- **Code:** `src/ttv/collector/openmanage.py` (`OpenManageSource`, the OME guard, profile join), `src/ttv/collector/redfish_gpu.py` (server-scan's GPU rules, the iDRAC reader and guard). Tests: `tests/test_collector_openmanage.py`, fixtures `tests/fixtures/openmanage/`.

### What one read does

1. **Log in.** `POST /api/SessionService/Sessions` with `{"UserName", "Password", "SessionType": "API"}`. The token comes back in `X-Auth-Token`; the body's `Id` is kept for logout. The password is re-read from its file on every read. HTTP 400/401/403 is `SourceError(PERMISSION_DENIED)`.
2. **List every server device.** `GET /api/DeviceService/Devices?$filter=Type eq 1000`, following `@odata.nextLink`. Only the link's path and query are used, on the configured origin. An entry whose `Type` is not 1000 is skipped and counted (`non_server_devices`).
3. **List every profile.** `GET /api/ProfileService/Profiles`, paged. On 404 it falls back to `/api/TemplateService/Profiles` (OME 3.2), and coverage records which path answered.
4. **Join, read GPU evidence** (below), then **log out** with `DELETE /api/SessionService/Sessions('<Id>')`.

The read raises `SourceError` and returns nothing usable when any of these happens: login fails; a listing page fails or is malformed; a `nextLink` names anything but the same list; or the listing misses the read deadline (`collector_timeout_seconds`).

Each server device becomes one `ManagerServer`:
- `serial_normalized` is `normalize_serial(DeviceServiceTag)`; a placeholder tag gives `""`.
- `source_id` is the device `Id`; `name` is `DeviceName`; `model` is `Model`.
- `profile_name` is the bound `ProfileName`, or None.
- `domain` is None: OME groups cost extra calls.
- `evidence` holds `device_id`, `last_inventory_time`, `profile_id`, `profile_state`, `profile_join` (`target_id`, `target_name` or `ambiguous`), `gpu_source` (`ome-inventory`, `redfish` or null), `gpu_rule`, `gpu_read_at`, and `gpu_note` when GPUs are unread.

### Profile binding

- **"Has a profile" means a profile targets the device.**
  - The join is `TargetId == Id`.
  - Only a profile with no `TargetId` (null or 0) falls back to `TargetName == DeviceName`, server-scan's join (`openmanage/provider.py@defe172`). That name must match exactly one device.
  - A `TargetId` naming a device outside the listing is counted `profiles_unmatched`. It never falls back to the name.
- **Any `ProfileState` binds, including 0 (unassigned).**
  - An unassigned profile normally has no target (`TargetId` 0, empty `TargetName`), so it binds nothing and is counted `profiles_untargeted`.
  - If OME ever reports state 0 *with* a target, the profile binds, because it targets the device. Its state stays in the evidence.
  - `ProfileState` and deploy jobs are evidence only; Jobs are never read.
- **A `TargetName` that matches several devices binds none of them.** Those devices get `profile_join: ambiguous` and `profile_unresolved`, so the engine records each as a `COLLECTOR_PROFILE_AMBIGUOUS` identity conflict (UNKNOWN), never as a DC charge. The read stays complete.
- **Several profiles on one device.** `TargetId` joins win, then the lowest profile `Id`. The others go to `other_profile_ids`.

### GPU evidence

For each device, in order:

1. **OME inventory.** Read `GET /api/DeviceService/Devices(<Id>)/InventoryDetails('<type>')` for each configured type (default `serverDeviceCards`).
   - A card is a GPU by server-scan's PCIe rule (`is_gpu_pcie_device`): not `Absent`, a GPU vendor, and a `Description` containing VGA, GPU, 3D or DISPLAY.
   - The rule is adapted to accept vendor *names* (NVIDIA, AMD / Advanced Micro Devices, Intel) as well as PCI IDs, because OME names vendors. The embedded Matrox VGA, NVSwitch bridges and NICs do not match (tested).
   - Any GPU found makes `gpu_source = ome-inventory`, with one model per card (its `Description`).
2. **No GPU listed.** An OME answer that lists no GPU is **not** taken as zero GPUs while `empty_inventory_means_no_gpu` is False (the default), and neither is a 400/404 or a failed read.
3. **iDRAC Redfish**, when `idrac_username` is set and the device has `DeviceManagement[0].NetworkAddress`. This is server-scan's rule:
   - `GET /redfish/v1/Systems`, then each system and its `Processors`. A GPU is `ProcessorType == "GPU"` and not `Absent`.
   - If that finds none and `pcie_fallback` is on (the default), screen the system's own `PCIeDevices` links with the card rule above. The model comes from server-scan's PCI-ID table, else the `Description`.
   - The result has `gpu_source = redfish` and `gpu_rule` set to `processors` or `pcie`.
4. **Otherwise** `gpu_models` is None (unread, never `()`), with a `gpu_note`, and is counted in `gpu_unread`.

**When the read is incomplete.** Only when a device or profile entry is malformed (`complete=False`), since then the listing is not whole. A GPU read that was attempted and failed (an OME error with no Redfish to fall back to, a Redfish error, an unusable BMC answer, or the deadline) leaves that server's `gpu_models=None`, counts in `gpu_read_failed`, and adds an error naming the device `Id` (capped at 50 lines). The read stays complete. GPUs unread only for lack of a source (Redfish not configured, no iDRAC address) are counted the same way.

**Cost bounds.**
- **Concurrency.** Per-device reads run under a semaphore (`gpu_concurrency`, default 8).
- **Cache.** Answers are cached per device `Id` and `LastInventoryTime` for at most `gpu_cache_ttl` (default 24 h). Failures are not cached.
- **Deadline.** Reads still running at the deadline stay unread for this cycle, so a cold cache warms over a few cycles.
- **Size limits.** A Redfish collection with more than `max_members` (128) members, or a paged one, leaves that server unread.
- **Lockout guard.** Three iDRACs refusing the account, with no success in the same read, stop Redfish for the rest of that read. server-scan's ADR-0016 records that repeated rejections IP-block the collector.

### Read-only enforcement (in code, tested)

**OME guard.** `openmanage.check_request` runs as an httpx request hook, before any transport.
- Requests go to the configured origin only. Userinfo, fragments and percent-encoded paths are refused.
- `POST` is allowed only to `/api/SessionService/Sessions`, with no query.
- `DELETE` is allowed only to `Sessions('<Id of the session this client opened>')`.
- `GET` is allowed only for:
  - the device list with exactly `$filter=Type eq 1000` (plus numeric `$top`/`$skip`);
  - the two profile paths (numeric `$top`/`$skip` only);
  - `InventoryDetails('<configured type>')` of one numeric device id.
- Everything else raises `UnsafeRequestError`.

**iDRAC guard.** `redfish_gpu.check_request`, also a request hook.
- `GET` only, HTTPS on port 443, to an iDRAC address in the current listing.
- No query, userinfo, fragment or percent-encoding.
- Paths: `/redfish/v1/Systems`, `/Systems/<id>`, `/Systems/<id>/Processors[/<id>]` and `/(Systems|Chassis)/<id>/PCIeDevices/<id>`. The segments `Actions`, `SessionService`, `Sessions` and `Oem` are refused.
- A link the BMC supplies is checked before it is requested, by server-scan's `validate_odata_id` and then the path allow-list.

**iDRAC auth is HTTP Basic on each GET.** server-scan's Redfish client logs in by creating a session (`POST …/Sessions`) and deletes it afterwards. Basic auth needs neither request:
- the guard can stay GET-only;
- no session can leak against the iDRAC session cap;
- it matches probe B4 (`curl -K`).

server-scan's iDRAC reset path (`redfish/idrac_reset.py`, its ADR-0050) is not copied.

**Secrets.** Passwords are read from their files on every read and never logged. Tests assert that the passwords, the OME token and the Basic header never appear in logs, errors, coverage or evidence. TLS verification is always on (`ca_bundle`, `idrac_ca_bundle`).

### Reused from server-scan (`defe172`, one-line provenance comment per block)

- **`openmanage/client.py`:** login, `X-Auth-Token`, `@odata.nextLink` paging and logout, adapted to the house client: guard hook, `get_json` retries, `SourceError`.
- **`openmanage/mapping.py`:** `_opt_str` and `_network_address`, copied.
- **`redfish/mapping.py`:**
  - copied: `GPU_PCI_VENDOR_IDS`, `GPU_DESCRIPTION_HINTS`, `BUILTIN_PCI_DEVICE_MODELS`, `is_absent`, `is_gpu_processor`, `pci_ids_from_manufacturer` and `pcie_device_refs`;
  - trimmed to the model string: `gpus_from_processors`, `pcie_device_to_gpu` and `gpus_from_pcie_devices`;
  - `is_gpu_pcie_device` also accepts vendor names.
- **`redfish/client.py`:** `validate_odata_id`.
- **Not reused:**
  - the profile-driven provider loop, which cannot list a server without a profile;
  - DGX tray folding (the GPUs of every system on one BMC are summed instead);
  - the `$expand`/`$select` PCIe scan and the `Chassis.PCIeDevices` walk (only the system's own `PCIeDevices` links are read, as in probe B4);
  - session login, and `idrac_reset.py`.

### Validation gates (production probe, `docs/discovery-probes.md` Part B, B3/B4)

1. **Which OME inventory type lists GPUs**, and whether its `Manufacturer`/`Description` pass the card rule. The fixtures assume `NVIDIA Corporation` and `… 3D controller`. A GPU whose description carries no VGA/GPU/3D/DISPLAY hint is missed in OME and falls back to Redfish. If OME lists every GPU, set `empty_inventory_means_no_gpu` so non-GPU servers stop costing an iDRAC read.
2. **How iDRAC reports GPUs:** whether `Processors` lists the fleet's GPUs as `ProcessorType: GPU`, and the shape of `PCIeDevice` (PCI ID or vendor name; hint present or not). **Risk:** a GPU server whose iDRAC shows its GPUs in neither form reads as `()` (no GPUs). server-scan and probe B4 would give the same answer.
3. **Access and TLS:** iDRAC HTTP Basic auth is enabled for a read-only account, and iDRAC certificates chain to `idrac_ca_bundle`. Verification is never turned off.
4. **Profile fields:** `TargetId` is the device `Id`; a `ProfileState` 0 profile carries no target; how often the `TargetName` fallback fires.
5. **Listing behaviour:** `Type eq 1000` is honoured (B3 `types == [1000]`); the `nextLink` shape and page size; the Viewer role can GET Profiles and `InventoryDetails`.
6. **Sessions:** the session `Id` is UUID-shaped (the logout guard accepts `[A-Za-z0-9._-]+`), and `MaxSessions` leaves headroom.
7. **Cold-cache cost:** inventory latency × server devices, plus iDRAC reads, fits `collector_timeout_seconds`.

### Configuration

Per OME in the managers file (`config/managers.example.yaml`): `inventory_types` (default `[serverDeviceCards]`), `empty_inventory_means_no_gpu` (default false), `pcie_fallback` (default true) and `gpu_concurrency` (default 8). The job passes `TTV_COLLECTOR_TIMEOUT_SECONDS` as the whole-read deadline and `TTV_HTTP_TIMEOUT_SECONDS` per request. `gpu_cache_ttl` (24 h) and `max_members` (128) stay code defaults.
