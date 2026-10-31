# ADR-0004: Profile-less GPU collector

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
- **A fallback domain fails** (login, deadline, no address, password file): the read returns `complete=False` with one error for that domain, and its servers keep `gpu_models=None` with the reason. Other domains are still read. The server listing itself is whole, since it comes from Central.
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
