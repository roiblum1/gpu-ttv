# ADR-0001: Architecture for Release 1

- **Status:** accepted 2026-10-30 (user approved the design and asked to start implementation)
- **Context:** AGENT.md §20.1, `docs/discovery.md`

## Decision

Three small components and one database. server-scan stays the inventory source of truth; each target cluster's existing DCGM exporter and Thanos querier supply GPU use. Nothing new runs on GPU nodes.

| Component | Runs as | Does |
| --- | --- | --- |
| `ttv-collector` | CronJob, every 15 min | Finds GPU servers in UCS Central and OpenManage that server-scan cannot see. Cisco: server-scan's UCS Central provider code with the name filter cleared. Dell: an OpenManage device listing plus server-scan's iDRAC Redfish GPU rule. Writes one observation per server per run. |
| `ttv-engine` | Deployment, one active replica (Postgres advisory lock), every 5 min | Builds an immutable snapshot from server-scan, the collector's latest complete run, and each target cluster's kube API and Thanos; joins identities; classifies every GPU with a pure function (ADR-0002); diffs the result into the interval ledger. Serves `/metrics`, `/healthz`, `/readyz`, `/explain/{asset_id}`. |
| Postgres | existing instance (open input I4) or chart-provided | The business ledger: assets, capacity intervals, milestones, identity conflicts, objectives, pricing snapshots, scan runs. |
| Grafana | existing instance or chart-provided | Dashboards and data sources as code with stable UIDs; Postgres for the ledger, Prometheus for live gauges. |

## Why

- **The ledger is Postgres, not Prometheus.** Stage durations, handovers and lower bounds need durable history that survives restarts, policy changes and price refreshes (§9).
- **One active engine with idempotent writes per scan** so HA never double-counts GPU-hours (§11).
- **Classification is one pure function** so each rule is unit-tested with fixtures and explainable per asset (§6).
- **The collector is separate from the engine:** it is slow, needs manager credentials and runs where server-scan's collectors already have network reach.
- **Value is computed at query time** from physical GPU-hours and a selected pricing snapshot, so a price refresh never rewrites history (§10).

## Build order (vertical slices)

1. server-scan → classification → ledger → Grafana (Redbull and DC maintenance queues).
2. Target clusters: kube API and Thanos (POPAI, Serving, productive).
3. `ttv-collector` (DC no-profile queue).
4. Pricing, home and queue dashboards, alerts.
5. Offline packaging (Helm, digest-pinned images), then at least one day of read-only dry run reviewed with the user before ledger writes are enabled.
