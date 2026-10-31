# Dry-run review (AGENT.md §20.6, §20.12)

Before the engine writes the ledger, it runs with `TTV_DRY_RUN=true` against production for **at least one day**, and the user reviews the output. This page is the procedure, the go/no-go criteria, the one change that enables writes, and the record the review leaves behind.

**Status:** procedure ready. No dry run has been run against production yet, so the record at the end is empty. What is verified so far is `lab-tested` only: the image ran a dry run against the sandbox server-scan (3,008 demo servers) and served `/readyz`, `/metrics/`, `/decisions` and `/explain` (ADR-0007).

## What a dry run writes

`TTV_DRY_RUN=true` is the chart default (`dryRun: true` in `deploy/helm/gpu-ttv/values.yaml`). Each scan reads every source, classifies every asset and plans the ledger changes. It then logs the result and serves it, but **writes no ledger rows**.

| Written in dry run (our own Postgres) | Never written in dry run |
| --- | --- |
| `schema_migrations` (migrations run at start) | `assets`, `capacity_intervals` |
| `stage_objectives` from the policy | `milestones` |
| pricing snapshots imported from `TTV_PRICING_DIR` | `identity_conflicts` |
| `scan_runs` (status `dry_run`, coverage, errors) | `gpu_model_aliases` |
| `source_state` (the grace counters), `serverscan_cache` | |
| the collector's reads (`collector_*` tables): the collector stores them whatever `TTV_DRY_RUN` says (ADR-0004) | |

What this means for the review:
- **The Grafana ledger pages stay empty.** Evidence & monitoring health still shows scan runs and source health.
- **"TTV engine scans stale" fires by design**, because a `dry_run` scan never updates the ledger (`docs/runbooks.md`).
- **Each scan plans against an empty ledger,** so `opens` in `scan.done` equals the number of buckets classified, and `closes` stays 0. Read `opens` as "buckets", not as churn.

Where the output is:
- **Logs:** one JSON object per line on stdout, with one `scan.done` per scan. Health probes, metric scrapes and each outgoing HTTP request are not logged, so the log stays at a few lines per scan; calls to `/decisions` and `/explain` are logged.
- **`GET /metrics/`:** `ttv_gpus{category,reason_code,stage_owner,site,cluster,gpu_model}`, `ttv_unknown_gpus`, `ttv_unpriced_gpus`, `ttv_identity_conflicts`, `ttv_scan_duration_seconds`, `ttv_dry_run` (1).
- **`GET /decisions`:** every asset's buckets from the last scan. It takes `?category=`, `?reason=` and `?limit=` (default 200, at most 10,000), and returns `matched` with the total.
- **`GET /explain/{asset}`:** the rules evaluated, the matching row, and the evidence.

The API has no authentication. Reach it with `oc port-forward`, not a Route.

## Procedure

Run every command against production as the platform operator; nothing here writes to a source.

1. **Configure the production values**, the same ones that will later write:
   - the real `policy` (its `policy_version` is what the ledger will record);
   - every target cluster under `clusters`, each with its reader token from `deploy/gpu-cluster-rbac/`;
   - the managers under `collector.managers`;
   - `database.existingSecret`.

   Keep `dryRun: true`. Install the chart (`deploy/helm/gpu-ttv/README.md`) and note the time: the 24-hour window starts at the first `scan.done` after the last configuration or image change.
2. **Prove the access is read-only** on each target cluster, and paste the output into the record:
   ```bash
   SA=system:serviceaccount:gpu-ttv-reader:gpu-ttv-reader
   oc auth can-i --list --as=$SA   # only get/list/watch, besides what every authenticated user has (self reviews)
   for verb in create update patch delete; do oc auth can-i $verb pods --as=$SA; done   # "no" four times
   oc auth can-i get secrets --as=$SA     # "no"
   ```
3. **Run the collector probe** once, in the collector pod. It reads every manager, prints every record and stores nothing:
   ```bash
   oc -n gpu-ttv exec deploy/gpu-ttv-collector -- ttv-collector --once --dry-run --servers > collector-probe.json
   ```
   For at least two servers per manager, compare the GPU count and profile with the manager's own UI.
4. **After 24 hours, collect the evidence.** Keep these files inside the air-gapped network, because they name assets and topology. Only counts leave it.
   ```bash
   oc -n gpu-ttv port-forward svc/gpu-ttv-engine 8080 &
   oc -n gpu-ttv logs deploy/gpu-ttv-engine --since=25h | jq -c 'select(.event=="scan.done")' > scans.jsonl
   curl -s localhost:8080/metrics/ | grep -E '^ttv_' > metrics.txt
   curl -s 'localhost:8080/decisions?limit=10000' > decisions.json
   psql "$TTV_DATABASE_URL" -c "SELECT status, count(*), max(finished_at - started_at) AS slowest
     FROM scan_runs WHERE kind = 'engine' AND started_at > now() - interval '24 hours' GROUP BY status"
   ```
5. **Check the invariants** on the production output:
   ```bash
   jq '[.assets[] | select(([.buckets[].gpus] | add) != .expected_gpus)] | length' decisions.json   # expect 0
   jq -r '.assets[].buckets[] | [.category, .reason, (.stage_owner // "-")] | @tsv' decisions.json | sort | uniq -c
   ```
6. **Spot-check at least 10 assets across every category** that has GPUs (§20.12):
   - at least one PRODUCTIVE;
   - one IDLE per stage owner present (DC, Redbull, POPAI, Serving);
   - one UNKNOWN per reason present;
   - one NOT_CHARGED.

   Pick them with `curl 'localhost:8080/decisions?category=IDLE&limit=20'` (or `?reason=...`), read `/explain/<asset>`, and compare it with the source: server-scan, the manager UI, or `oc get node/pods` on the target cluster. Record each one in the table below.
7. **Review the alerts** in Grafana (Alerting > Alert rules). In dry run, only the rules on `scan_runs`, `source_state` and `collector_*` mean anything:
   - "TTV engine scans stale" fires by design;
   - "TTV source failing past grace" and "TTV collector manager stale" must match what the logs show;
   - the UNKNOWN-share, node-not-found and stage-breach rules read the empty ledger and stay quiet or NoData.

   Check that no notification left Grafana (destinations stay unconfigured, ADR-0006).
8. **Review with the user** against the criteria below and fill in the record.

## Go/no-go criteria

Every item must hold. If one fails, fix it. If the fix changes the configuration, the policy or the image, the 24-hour window restarts.

| # | Criterion | How it is checked |
| --- | --- | --- |
| 1 | At least 24 h of dry run with the exact configuration, policy version and image digest that will write | `scans.jsonl` time span; `engine.start` log line; Deployment image |
| 2 | Scans keep pace: at least 95% of the expected scans finished (≥ 274 of 288 at 5 min), none `failed` in the final 6 h, and the slowest scan is shorter than the interval | `scan_runs` query; `ttv_scan_duration_seconds` |
| 3 | Every target cluster is read: no `CLUSTER_NOT_OBSERVED` or `CLUSTER_UNREACHABLE` for a configured cluster in the final 6 h; `cluster_sources_failed` is 0 in steady state | `metrics.txt`, `scans.jsonl` |
| 4 | server-scan is fully read: `pending_detail` reaches 0 and stays there; every `gpu_inventory_unread` server is listed and explained | `scans.jsonl`, `/decisions` |
| 5 | Every manager has a complete collector read in the last hour, and the probe's GPU counts match the manager UI for the sampled servers | `collector_reads`; step 3 |
| 6 | Invariant: per asset, bucket GPUs sum to `expected_gpus` (zero violations) | step 5 |
| 7 | Every UNKNOWN reason with GPUs is explained, and is either fixed or accepted in writing by the user with a reason. Unknown is never zero: it stays visible | `metrics.txt`, `/decisions?category=UNKNOWN` |
| 8 | Identity conflicts: the count is stable over the window, and each conflict kind present is understood (no silent merge) | `ttv_identity_conflicts`, `/decisions?reason=IDENTITY_CONFLICT` |
| 9 | Spot check: at least 10 assets covering every category with GPUs; every one matches the source, or the mismatch is fixed and re-checked | step 6 table |
| 10 | Read-only verified: cluster RBAC is only get/list/watch (step 2); manager and server-scan accounts are the approved read-only ones (C4: read-only enforced in code) | step 2 output; `docs/discovery.md` |
| 11 | No pod restarts, OOM kills or probe failures in the window; memory is well under the limit | `oc get pods`, `oc describe` |
| 12 | Alerts behave as in step 7, and nothing was sent | Grafana |
| 13 | The policy that will write is approved: the I1 stage objectives are approved, or accepted as provisional in writing | the record |

**No-go** if any item fails. Unknown or unexplained output is a no-go, not a pass.

## Enabling ledger writes (go)

The only change is one value: `dryRun: false` in the production values file of the `gpu-ttv` release. It renders `TTV_DRY_RUN=false` in the engine Deployment. Commit it with a link to the signed record below, then sync it in Argo CD or let the user run `helm upgrade`. The pod template changes, so the engine restarts (`Recreate`).

```yaml
# values-production.yaml
dryRun: false   # dry-run review signed off <date>, docs/dry-run-review.md
```

After the change:
- the engine logs `engine.start` with `"dry_run": false`, and `ttv_dry_run` is 0;
- `scan_runs.status` is `ok` or `partial`;
- `capacity_intervals` gains rows, and the Grafana pages fill;
- "TTV engine scans stale" resolves within one evaluation.

The ledger starts at the first writing scan. The dry-run day is not backfilled, and stage clocks that began before it are lower bounds (`≥`).

**Stopping writes again:** set `dryRun: true`. Nothing is deleted: intervals stay as written, and the open ones are reconciled by the next writing scan.

## Review record

Fill one block per review. The release is ready for writes only with a filled block signed by the user.

```
Window (UTC):            <start> → <end>
Image:                   <registry>/gpu-ttv/gpu-ttv@sha256:<digest>
Chart / values commit:   <commit>
policy_version:          <version>
Target clusters read:    <names>
Managers read:           <names>
Scans: expected / done / failed / slowest:   <n> / <n> / <n> / <s>
UNKNOWN by reason (GPUs), each with its explanation or written acceptance:
Identity conflicts (kinds, count):
Criteria 1-13:           <pass/fail each>
Read-only proof (step 2):  <pasted output>

Spot check (≥ 10 assets):
| asset | category / reason / owner from /explain | what the source shows | match |

Decision:                go / no-go
Reviewed with:           <user>, <date>
Writes enabled by:       <commit of dryRun: false>
```
