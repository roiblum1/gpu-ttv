# ADR-0001: Architecture for Release 1

- **Status:** accepted 2026-10-30 (user approved the design and asked to start implementation)
- **Updated 2026-11-25** ([ADR-0008](0008-drop-dc-stage-and-collector.md)): the `ttv-collector` component and the DC stage were removed. server-scan is the only inventory source.
- **Context:** AGENT.md §20.1, `docs/discovery.md`

## Decision

One engine, one database and Grafana. server-scan stays the inventory source of truth, and the only one; each target cluster's existing DCGM exporter and Thanos querier supply GPU use. Nothing new runs on GPU nodes.

| Component | Runs as | Does |
| --- | --- | --- |
| `ttv-engine` | Deployment, one active replica (Postgres advisory lock), every 5 min | Builds an immutable snapshot from server-scan and each target cluster's kube API and Thanos; joins identities; classifies every GPU with a pure function (ADR-0002); diffs the result into the interval ledger. Serves `/metrics`, `/healthz`, `/readyz`, `/explain/{asset_id}`. |
| Postgres | existing instance (open input I4) or chart-provided | The business ledger: assets, capacity intervals, milestones, identity conflicts, objectives, pricing snapshots, scan runs. |
| Grafana | existing instance or chart-provided | Dashboards and data sources as code with stable UIDs; Postgres for the ledger, Prometheus for live gauges. |

Until 2026-11-25 a fourth component, `ttv-collector`, read UCS Central and OpenManage for GPU servers server-scan cannot see (ADR-0004, superseded). It was removed with the DC stage.

## Why

- **The ledger is Postgres, not Prometheus.** Stage durations, handovers and lower bounds need durable history that survives restarts, policy changes and price refreshes (§9).
- **One active engine with idempotent writes per scan** so HA never double-counts GPU-hours (§11).
- **Classification is one pure function** so each rule is unit-tested with fixtures and explainable per asset (§6).
- **No inventory code of our own.** server-scan already reads every manager; servers it does not list are out of scope (ADR-0008).
- **Value is computed at query time** from physical GPU-hours and a selected pricing snapshot, so a price refresh never rewrites history (§10).

## Build order (vertical slices)

1. server-scan → classification → ledger → Grafana (Redbull's queue, which since ADR-0008 includes maintenance and broken hardware).
2. Target clusters: kube API and Thanos (POPAI, Serving, productive).
3. `ttv-collector` (DC no-profile queue). Built 2026-10-31, `fixture-tested` only; removed 2026-11-25 (ADR-0008).
4. Pricing, home and queue dashboards, alerts.
5. Offline packaging (Helm, digest-pinned images; ADR-0007), then at least one day of read-only dry run reviewed with the user before ledger writes are enabled (`docs/dry-run-review.md`).
