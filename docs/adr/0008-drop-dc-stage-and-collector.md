# ADR-0008: Drop the DC stage and the profile-less collector

- **Status:** accepted 2026-11-25 by the user, who owns AGENT.md. Amended the same day by [ADR-0009](0009-five-teams-and-manager-dashboards.md): five teams, and maintenance is in repair (not charged). Supersedes ADR-0004 and the DC parts of ADR-0001, ADR-0002, ADR-0005, ADR-0006 and ADR-0007. AGENT.md (revision 4) records the decision in §0.
- **Context:** AGENT.md §0.1, §0.3, §0.7 (I1, I6, I8), §20.2; ADR-0004 (the collector); `docs/discovery.md`, "Managers and the profile-less collector" and C1.
- **Code:** `src/ttv/models.py` (`Stage`, `Reason`, `Flag`), `src/ttv/classify.py` (rows 6a, 6b and 13), `src/ttv/config.py` (`Policy._no_dc_stage`), `migrations/0006_drop_collector.sql`. Tests: `tests/test_classify.py`, `tests/test_ledger_plan.py`, `tests/test_engine_pg.py`.

## Context

Until now the platform charged idle GPU-hours to four teams. DC owned GPU servers that UCS Central or OpenManage knew but server-scan could not see, because they had no qualifying `ocp*` profile. Finding them needed a separate collector (ADR-0004) with its own manager credentials, TLS trust, Helm workload, tables, alerts and dashboard panels. That collector was only ever `fixture-tested`: there was no UCS Central, OpenManage or iDRAC in the lab, and its production probe (`docs/discovery-probes.md`, former Part B) was never run.

On 2026-11-25 the user asked for a much simpler platform:

> "I want to make this much simpler. Remove the part of DC team. Focus on the ServerScanner — if there is a GPU node there which is not inside a cluster, Redbull costs money, and from then the rest is the same."

Asked follow-up questions, the user chose:
- Broken hardware, which DC owned under I8, becomes **Redbull's**, with the same reason codes.
- The profile-less collector is **deleted**, not kept dormant. Git history keeps it.

## Decision

**Three accountable teams: Redbull, POPAI and Serving.** server-scan is the only inventory source.

| Stage | Team | Holds the GPUs while… |
| --- | --- | --- |
| `PROVISIONING` | Redbull | The server is in server-scan with GPUs and is not yet a Ready, schedulable node in a target cluster. This includes maintenance, critical hardware health and an unreachable BMC. |
| `GPU_PLATFORM` | POPAI | Unchanged. |
| `SERVING` | Serving | Unchanged. |

**Where the pipeline starts.** A GPU server's pipeline starts when it first appears in server-scan, in Redbull's stage. This is the rule that already applied to standalone DGX servers, now applied to every server. Redbull's stage clock is `openshift.claim_changed_at` if set, else server-scan's `created_at` as a lower bound.

**Broken hardware is Redbull's (I8).** The reason codes stay the same; only the stage changes from `PRE_PROVISIONING` to `PROVISIONING`.

| ADR-0002 row | Condition | Reason | Stage clock |
| --- | --- | --- | --- |
| 6a | Maintenance on an INSTALLED server | `MAINTENANCE` | `maintenance.created_at` |
| 6a | Maintenance on a server that is not INSTALLED | `MAINTENANCE` | Redbull's normal clock, because Redbull already held the server |
| 6b | Critical health or unreachable BMC, server not INSTALLED | `HARDWARE_CRITICAL`, `BMC_UNREACHABLE` | Redbull's normal clock |
| 13 | Not-Ready node in a target cluster with critical hardware or an unreachable BMC | `HARDWARE_CRITICAL`, `BMC_UNREACHABLE` | the node's `Ready` transition, a lower bound |

The stage clock only dates a server the ledger has never seen. For a known server, the ledger keeps one Redbull episode running across these reason changes, because the stage does not change. A server that moves into Redbull's stage from another stage starts a new episode at that scan (ADR-0002, "Stage clocks").

**Removed:**
- **Stage** `PRE_PROVISIONING` and its I1 objective (DC 3 d). The objectives are now Redbull 2 d, POPAI 1 d, Serving 7 d, still provisional.
- **Reason codes** `PROFILE_NONE`, `OTHER_INTENDED_USE` and `HANDOVER_PENDING`, plus `PROFILE_ASSIGNED_NOT_APPLIED` and `PROFILE_APPLY_FAILED` (dropped from R1 on 2026-10-30 and never coded). **Flag** `handover_overdue`.
- **ADR-0002 rows 3–5.** Their numbers stay unused, so rows 6–22 keep their numbers in the code trace, `/explain`, the alerts and the docs.
- **The collector:** `src/ttv/collector/`, the `ttv-collector` console script, its Helm workload and values, `config/managers.example.yaml`, and the settings `TTV_MANAGERS_FILE`, `TTV_COLLECTOR_TIMEOUT_SECONDS`, `TTV_COLLECTOR_INTERVAL_SECONDS` and `TTV_COLLECTOR_STALE_AFTER`. The policy's `collector:` section (`ocp_profile_pattern`, `handover_grace`) is gone, and `/explain` has no `collector` field.
- **Migrations** 0003–0005, which created the collector tables. They are deleted from the repository.
- **Alerts** "TTV collector manager stale" and "TTV handover overdue" (8 rules → 6, ADR-0006).
- **Dashboards:** every DC column and every collector panel.

## Consequences

**What is no longer seen.** These were visible only through the collector. They are now out of scope: nobody is charged for them, and no panel shows them.
- **GPU servers with no profile** in UCS Central or OpenManage: DC's former queue.
- **GPU servers with a non-`ocp` profile:** "other intended use" (I6, now closed).
- **Handover gaps:** an `ocp` profile applied in the manager but not yet ingested by server-scan (`HANDOVER_PENDING`). Such a server now simply appears in Redbull's queue when server-scan lists it.

Absence from server-scan was never proof that a server does not exist, and the fleet totals now only cover what server-scan lists. The dashboards' scope note should say so.

**Time before server-scan is unknown, never zero.** Intake and pre-provisioning happen before a server appears in server-scan. That time is not measured:
- Redbull's episode for a new server outside a cluster starts at `claim_changed_at` or at server-scan's `created_at`. `created_at` is marked as a lower bound (`stage_entered_lower_bound = true`), so durations read `≥ X`.
- The `in_serverscan` milestone is exact as a fact about server-scan. It is not a receipt or acceptance date (ADR-0005).
- A server whose profile was assigned but not applied is already in server-scan (C1), so that wait was already Redbull's. Nothing changes there.

**Existing databases.** `migrations/0006_drop_collector.sql` drops `collector_servers`, `collector_reads`, `collector_first_seen`, `collector_profile_first_seen` and `collector_managers` with `IF EXISTS`, so it runs on a database that applied 0003–0005 and on a new one. It also deletes the `collector:<manager>` rows from `source_state`, so Evidence stops listing them. Ledger rows written with stage `PRE_PROVISIONING` or the removed reasons stay as history under their own `policy_version`. The engine closes any that are still open: a server-scan server's at the next writing scan, and a former collector-only asset's once it has been missing from complete server-scan listings for `tombstone_after` (24 h). No production ledger has been written yet (the dry run is still pending), so only lab ledgers hold such rows.

**Old policies are refused.** A policy whose `teams` or `stage_objectives` still names `PRE_PROVISIONING` fails validation with "the DC stage (PRE_PROVISIONING) was removed (ADR-0008): delete it from teams and stage_objectives". A policy that still has a `collector:` section fails as an unknown key. The engine does not start with either, so an old file cannot silently charge DC's former servers to nobody.

**Less to install and operate.** There are no manager credentials, no manager TLS trust, no egress to UCS Central, OpenManage or iDRAC, and no second workload. The image has two entry points: `ttv-engine` and `ttv-pricing`.

**Evidence.** The October lab evidence (sandbox server-scan, the OpenShift 4.22 workshop cluster) predates this change. Those systems no longer exist, and the collector was never lab-tested anyway. Past `lab-tested` labels stay as history. The new rules are `fixture-tested` (the tests listed under **Code** above).

**Open questions for the user.** None blocks R1.
- **I1:** the three stage objectives are still provisional.
- **Fleet completeness:** without the collector, nothing reports how many GPU servers sit outside server-scan. If the CEO view ("what did we buy", E01) must cover them, procurement records (R3) are the way in, not a collector.
- **Maintenance outside the target clusters** *(settled by ADR-0009: maintenance is never charged)*: row 6a runs before row 10, so a server in maintenance that is INSTALLED in a cluster outside the target list (I7, "not measured") is charged to Redbull, as it was charged to DC before. If such servers should stay "not measured", row 6a needs a target-cluster condition.

## What would bring it back

Reopen this decision, with the user, if any of these becomes true:
- Idle GPU servers outside server-scan turn out to be a material share of the fleet. The production probe of the collector (former Part B of `docs/discovery-probes.md`, in git history) would measure it first.
- A team again becomes accountable for servers before they reach server-scan.
- server-scan stops ingesting some qualifying servers, so handover gaps need watching.

Bringing the collector back means restoring `src/ttv/collector/`, migrations 0003–0005 (as new migration numbers after 0006), the `PRE_PROVISIONING` stage, rows 3–5, the two alerts and their panels from git history. ADR-0004 describes that design and its validation gates.
