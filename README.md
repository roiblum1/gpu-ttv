# GPU Time-to-Value

[![CI](https://github.com/roiblum1/gpu-ttv/actions/workflows/ci.yml/badge.svg)](https://github.com/roiblum1/gpu-ttv/actions/workflows/ci.yml)

Follows every GPU server from the moment it exists in the data centre to the moment it does useful work, and answers the question *whose queue is this GPU waiting in, and for how long?*

Each GPU is in one of four categories at any time:
- **productive:** Run:ai training or inference, or a model server with real traffic;
- **idle:** waiting in a team's stage;
- **unknown:** the evidence is missing, and it is never counted as zero;
- **not charged:** excluded on purpose.

Idle GPU-hours are charged to the team that owns the current stage:

| Stage | Owner (configurable) | Holds the GPUs while… |
| --- | --- | --- |
| Pre-provisioning | DC | the server has no profile, or is in maintenance, or its hardware is critical |
| Provisioning | Redbull | it is in [server-scan](https://github.com/team-redbull/server-scan) but not yet a Ready, schedulable node |
| GPU platform | POPAI | the node is Ready but its GPUs are not advertised or not reporting to DCGM |
| Serving | Serving | the GPUs are ready but unused: no workload, not ready, or no real demand |

Idle time past a stage objective is reported as **overdue**. Value is shown as the **AWS On-Demand equivalent in USD** (Linux, shared tenancy, us-east-1): a comparison, not a bill or a loss ([ADR-0003](docs/adr/0003-aws-pricing.md)).

The binding brief is [`AGENT.md`](AGENT.md). The design is in [ADR-0001](docs/adr/0001-architecture.md) (architecture), [ADR-0002](docs/adr/0002-classification-rules.md) (every classification rule), [ADR-0003](docs/adr/0003-aws-pricing.md) (AWS pricing), [ADR-0004](docs/adr/0004-profile-less-collector.md) (profile-less collector) and [ADR-0007](docs/adr/0007-deployment.md) (packaging and deployment).

## How it works

```
server-scan API (inventory) ─────────────┐
target clusters, read-only:              │
  kube API  (nodes, GPU pods, owners)  ──┼──▶ ttv-engine (every 5 min, one active replica)
  Thanos    (DCGM, serving counters)   ──┤       snapshot → classify (pure) → interval ledger
                                         │        /healthz /readyz /decisions /explain/{asset} /metrics
UCS Central, OpenManage (+ iDRAC),       │               │
  read-only ──▶ ttv-collector (15 min) ──┴──────▶ Postgres ──▶ Grafana (dashboards as code)
```

- **Read-only everywhere.** Each client allows only the GET paths it needs and refuses everything else before a request is sent.
- **Classification is one pure function.** Each rule is first-match-wins, and `/explain/{asset}` shows which rules were evaluated and the evidence behind the result.
- **The ledger stores intervals of physical GPU counts.** Money is computed when queried, from a dated price snapshot, so refreshing prices never rewrites history.
- **A failing source keeps its last good data** for a grace period, then turns UNKNOWN. A partial listing never deletes anything.

## Status: Release 1, all five slices built; the production dry run is pending

| Area | State | Evidence |
| --- | --- | --- |
| server-scan client (`src/ttv/sources/serverscan.py`) | GET-only guard, ETag, detail cache with budget | `fixture-tested`; `lab-tested` against a sandbox (3,008 servers) |
| Classifier (`classify.py`, `gpu_rows.py`, `cluster_view.py`) | ADR-0002 rows 1–22 | `fixture-tested`, incl. AGENT.md §17's 12 GPU-hour check |
| Ledger (`ledger.py`, `store.py`, `migrations/`) | Intervals, stage episodes, tombstones, conflicts, milestones | `fixture-tested` on Postgres 17, incl. §17's 24 → 16 overdue check |
| Engine + API (`engine.py`, `api.py`, `main.py`) | Scan loop, leader lock, dry-run default, per-cluster deadlines and grace; `/decisions` lists every asset's buckets for the dry-run spot check | `lab-tested`: sandbox + one cluster in 3.6 s, idempotent re-scan; `/decisions` `fixture-tested` (`tests/test_api_decisions.py`) and `lab-tested` in the image |
| Target clusters (`sources/kube.py`, `sources/thanos.py`) | Nodes, GPU pods, owner chains; DCGM, activity, serving counters | `lab-tested` on OpenShift 4.22 with KServe/llm-d; GPU rows on **simulated** GPUs (`deploy/lab/`) |
| Grafana (`grafana/`) | The R1 pages of §20.11: Home, Queues (E03), Delivery (E02), GPU use (E12), Asset drill-down, Evidence (E18), and the R2/R3 pages in their missing-input state; shared design conventions; a panel contract per panel (`config/panel-contracts/`), checked by `tests/test_panel_contracts.py`; every query run by `tests/test_dashboards_sql.py` | `fixture-tested` on Grafana 12.4; `lab-tested` on the lab ledger and reviewed in a browser at 1440 px with the side menu docked (`tests/test_dashboard_layout.py`) |
| Profile-less collector (`src/ttv/collector/`, `ttv-collector`) | UCS Central (+ UCS Manager GPU fallback) and OpenManage (+ iDRAC Redfish GPU fallback) readers, read-only in code with TLS verified; the job; rows 3–5 in the engine | `fixture-tested` only (synthetic UCS XML, OME and Redfish JSON); validation gates in ADR-0004 await the production probe |
| AWS pricing in USD (`src/ttv/pricing/`, `ttv-pricing`) | Offline snapshots (capture, verify, import, rollback), GPU catalogue and instance map, present-day and historical value, `ttv_unpriced_gpus`, Queues value panels | Snapshot of the real us-east-1 price list published 2026-10-29 (`documented`); `fixture-tested` incl. §17 linear pricing; `lab-tested` on the lab ledger and Grafana 12.4. Proxies and size rule approved 2026-10-31 |
| Alerts (`grafana/provisioning/alerting/`, `docs/runbooks.md`) | 8 Grafana-managed rules on the ledger: engine, sources and collector managers stale, UNKNOWN share high or growing, handover overdue, node not found, stage objective breached per owner. Notification destinations unconfigured and muted until supplied (§14) | `fixture-tested` (`tests/test_alert_rules_pg.py`, `tests/test_alert_rules.py`); `lab-tested`: provisioned in Grafana 12.4 and evaluated on the lab ledger, nothing sent (ADR-0006) |
| Container image (`Containerfile`) | One image for `ttv-engine`, `ttv-collector` and `ttv-pricing`; digest-pinned bases; dependencies by `uv.lock` hash from a mirror or wheelhouse; non-root, read-only root filesystem | `lab-tested`: built with the network off from a wheelhouse; all three entry points ran with no network and an arbitrary UID; a dry run against the sandbox (3,008 servers) served `/readyz`, `/metrics/`, `/decisions`, `/explain`; built in an OpenShift 4.22 cluster (amd64) from a fixed commit |
| Helm chart (`deploy/helm/gpu-ttv/`), cluster RBAC (`deploy/gpu-cluster-rbac/`) | Engine and collector Deployments, ConfigMaps, Secrets by name only, `dryRun: true` default, probes, NetworkPolicy, ServiceMonitor, Grafana sidecar ConfigMaps rendered from `grafana/`; per-cluster read-only RBAC with discovered CRD kinds | `fixture-tested` (`helm lint`/`template`, `tests/test_helm_charts.py`); `gpu-ttv` `lab-tested`: installed and upgraded on OpenShift 4.22 in dry run, under the `restricted-v2` SCC, probes passing, ServiceMonitor scraped through the NetworkPolicy (ADR-0007); the cluster RBAC chart is not installed anywhere yet |
| Offline delivery (`offline/`), GitLab CI (`.gitlab-ci.yml`) | Image lock, mirror/import (skopeo, digests preserved), wheelhouse, SBOMs, bundle with `SHA256SUMS`; internal build pipeline | `lab-tested` in parts (wheelhouse, one image mirrored with its digest checked, SBOMs); import `fixture-tested` against a stub; no full bundle or registry import yet |
| Dry-run review (`docs/dry-run-review.md`) | Procedure, go/no-go criteria, the one change that enables writes, record template | Not yet run against production |

Nothing here is `production-verified` yet. Evidence labels are defined in [`docs/discovery.md`](docs/discovery.md).

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env                                  # set TTV_SERVERSCAN_URL; TTV_DATABASE_URL to keep results
cp config/policy.example.yaml config/policy.yaml      # owners, stage objectives, target clusters
cp config/clusters.example.yaml config/clusters.yaml  # optional: target clusters to read
uv run ttv-engine --print-config   # effective settings (secrets masked), policy and clusters
uv run ttv-engine --once           # one scan, summary on stdout
uv run ttv-engine                  # scan loop + API on :8080
```

The profile-less collector is a separate job. Point `TTV_MANAGERS_FILE` at a copy of `config/managers.example.yaml`, then:

```bash
uv run ttv-collector --once --dry-run --servers   # read every manager, print each record, store nothing
uv run ttv-collector                              # every 15 min, store reads for the engine
```

Prices are never fetched by the engine. On a connected machine, `uv run ttv-pricing capture --region us-east-1` writes a new snapshot under `config/pricing/snapshots/`; carry it in and restart (or `ttv-pricing import`). Roll back by setting `TTV_PRICING_SNAPSHOT` to an older id ([ADR-0003](docs/adr/0003-aws-pricing.md)).

`TTV_DRY_RUN=true` (the default) classifies and plans without writing the ledger. Turn it off only after a reviewed dry run of at least a day: [`docs/dry-run-review.md`](docs/dry-run-review.md) (AGENT.md §20.6). To view the dashboards, see [`grafana/README.md`](grafana/README.md).

## Deployment

```bash
podman build -t gpu-ttv:dev .                                   # one image, three entry points
podman run --rm gpu-ttv:dev ttv-collector --print-config
helm template gpu-ttv deploy/helm/gpu-ttv -f values.yaml          # review what would be installed
```

- [`deploy/helm/gpu-ttv/`](deploy/helm/gpu-ttv/README.md): the engine and collector chart. It starts in dry run, and its values never hold a secret.
- [`deploy/gpu-cluster-rbac/`](deploy/gpu-cluster-rbac/README.md): read-only access on each target cluster, with CRD kinds discovered per cluster.
- [`offline/`](offline/README.md): the air-gapped bundle, from connected preparation through import, install, upgrade and rollback, with the network flows.
- [`docs/dry-run-review.md`](docs/dry-run-review.md): the at-least-one-day dry run, the go/no-go criteria and the change that enables ledger writes.

Installing into a cluster is always the user's step.

## Configuration

| File | What it holds |
| --- | --- |
| `.env` ([example](.env.example)) | Runtime settings: endpoints, token files, CA bundles, database, cadences, grace periods |
| `config/policy.yaml` ([example](config/policy.example.yaml)) | Versioned business policy: stage owners, objectives, target clusters, exclusions, serving counters, Run:ai kinds, pools, teams |
| `config/clusters.yaml` ([example](config/clusters.example.yaml)) | One read-only connection per target cluster: kube API, Thanos route, token file, CA bundles, expected cluster ID |
| `config/managers.yaml` ([example](config/managers.example.yaml)) | UCS Central and OpenManage connections for the collector: host or URL, account, password file, CA bundles, optional UCS Manager and iDRAC fallback accounts, OME GPU-inventory switches |
| [`config/pricing/gpu-instance-map.yaml`](config/pricing/gpu-instance-map.yaml) | Versioned GPU model → AWS instance map (`exact`, `proxy`, `unpriced`, with reasons) |
| `config/pricing/snapshots/*.json` | Immutable, checksummed AWS price snapshots, imported at startup (`TTV_PRICING_DIR`); `TTV_PRICING_SNAPSHOT` pins the present-day one |

## Development

```bash
podman run -d --rm --name ttv-test-pg -e POSTGRES_USER=ttv -e POSTGRES_PASSWORD=ttvtest -e POSTGRES_DB=ttv \
  -p 55433:5432 docker.io/library/postgres:17
TTV_TEST_DSN=postgresql://ttv:ttvtest@localhost:55433/ttv bash .claude/skills/gate/gate.sh
```

The gate runs `ruff check`, `ruff format --check`, `ty check` and `pytest`. CI runs the same on Python 3.12 and 3.13 with Postgres 17. Without `TTV_TEST_DSN` the Postgres tests are skipped, not passed. See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions.

## Repository layout

```
src/ttv/            engine package
  sources/          read-only clients: server-scan, kube API, Thanos (+ shared HTTP plumbing)
  collector/        profile-less collector: UCS Central, OpenManage/iDRAC readers, ttv-collector job
  classify.py       server-level rules (pure)
  gpu_rows.py       GPU-level rules 15–22 (pure)
  ledger.py         scan decisions → interval opens/closes (pure)
  store.py          Postgres ledger
  engine.py         one scan: fetch → classify → plan → apply
  pricing/          GPU catalogue, instance map, price snapshots, capture/import CLI (ttv-pricing)
migrations/         plain SQL schema
config/             policy and cluster examples; pricing map and snapshots
grafana/            data sources and dashboards as code
deploy/helm/        the gpu-ttv Helm chart (engine, collector, Grafana provisioning)
deploy/gpu-cluster-rbac/  read-only access chart for each target cluster
deploy/lab/         lab-only manifests (fake GPUs on a cluster without GPUs)
offline/            image lock, mirror/import, wheelhouse, SBOM and bundle scripts
Containerfile       the runtime image; .gitlab-ci.yml builds it inside the air-gapped network
docs/               discovery findings, probe runbooks, ADRs
tests/              unit, fixture and Postgres tests
```

## Documentation

- [`AGENT.md`](AGENT.md): the binding brief. Section 0 overrides Section 20, which overrides Sections 1–19.
- [`docs/discovery.md`](docs/discovery.md): verified source facts, user decisions, contradictions and open questions.
- [`docs/discovery-probes.md`](docs/discovery-probes.md): read-only probe runbooks for the air-gapped production environment.
- [`docs/adr/`](docs/adr/): architecture, classification, pricing, collector, delivery-clock, alerting and deployment decisions.
- [`docs/dry-run-review.md`](docs/dry-run-review.md): the §20.6 dry-run review that gates ledger writes (§20.12).
- [`offline/README.md`](offline/README.md): air-gapped delivery and install.
- [`deploy/lab/README.md`](deploy/lab/README.md): exercising the GPU rules on a cluster without GPUs.

## License

All rights reserved (see `pyproject.toml`). No open-source licence has been chosen.
