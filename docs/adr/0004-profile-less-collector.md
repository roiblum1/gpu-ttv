# ADR-0004: Profile-less GPU collector

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
- **A `TargetName` that matches several devices binds none of them.** Those devices get `profile_join: ambiguous`, and the read is `complete=False`, so an unknown binding never silently becomes a DC charge.
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

**When the read is incomplete.** `complete=False` when a GPU read was attempted and failed: an OME error with no Redfish to fall back to, a Redfish error, an unusable BMC answer, or the deadline. Those servers keep `gpu_models=None`, and `errors` names the device `Id` (capped at 50 lines). GPUs unread only for lack of a source (Redfish not configured, no iDRAC address) are counted but do not make the read incomplete.

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

### Not in configuration yet

`inventory_types`, `empty_inventory_means_no_gpu`, `pcie_fallback`, `gpu_concurrency`, `gpu_cache_ttl`, `request_timeout_seconds` and `max_members` are `OpenManageSource` constructor arguments. Neither `OpenManageConnection` nor `Settings` carries them. Whoever wires the collector decides which to expose, and the `serverDeviceCards` gate (1) needs at least `inventory_types` and `empty_inventory_means_no_gpu`.
