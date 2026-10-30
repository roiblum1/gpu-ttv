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

Idle time past a stage objective is reported as **overdue**. Value is shown as the **AWS On-Demand equivalent in USD** (slice 4).

The binding brief is [`AGENT.md`](AGENT.md). The design is in [ADR-0001](docs/adr/0001-architecture.md) (architecture) and [ADR-0002](docs/adr/0002-classification-rules.md) (every classification rule).

## How it works

```
server-scan API (inventory) ─────────────┐
target clusters, read-only:              │
  kube API  (nodes, GPU pods, owners)  ──┼──▶ ttv-engine (every 5 min, one active replica)
  Thanos    (DCGM, serving counters)   ──┘       snapshot → classify (pure) → interval ledger
                                                  /healthz /readyz /explain/{asset} /metrics
                                                         │
                                                     Postgres ──▶ Grafana (dashboards as code)
```

- **Read-only everywhere.** Each client allows only the GET paths it needs and refuses everything else before a request is sent.
- **Classification is one pure function.** Each rule is first-match-wins, and `/explain/{asset}` shows which rules were evaluated and the evidence behind the result.
- **The ledger stores intervals of physical GPU counts.** Money is computed when queried, from a dated price snapshot, so refreshing prices never rewrites history.
- **A failing source keeps its last good data** for a grace period, then turns UNKNOWN. A partial listing never deletes anything.

## Status: Release 1, slices 1–2 of 5

| Area | State | Evidence |
| --- | --- | --- |
| server-scan client (`src/ttv/sources/serverscan.py`) | GET-only guard, ETag, detail cache with budget | `fixture-tested`; `lab-tested` against a sandbox (3,008 servers) |
| Classifier (`classify.py`, `gpu_rows.py`, `cluster_view.py`) | ADR-0002 rows 1–2b and 6–22 (rows 3–5 wait for the slice-3 collector) | `fixture-tested`, incl. AGENT.md §17's 12 GPU-hour check |
| Ledger (`ledger.py`, `store.py`, `migrations/`) | Intervals, stage episodes, tombstones, conflicts, milestones | `fixture-tested` on Postgres 17, incl. §17's 24 → 16 overdue check |
| Engine + API (`engine.py`, `api.py`, `main.py`) | Scan loop, leader lock, dry-run default, per-cluster deadlines and grace | `lab-tested`: sandbox + one cluster in 3.6 s, idempotent re-scan |
| Target clusters (`sources/kube.py`, `sources/thanos.py`) | Nodes, GPU pods, owner chains; DCGM, activity, serving counters | `lab-tested` on OpenShift 4.22 with KServe/llm-d; GPU rows on **simulated** GPUs (`deploy/lab/`) |
| Grafana (`grafana/`) | Queues and Evidence dashboards, provisioned data sources | `fixture-tested` on Grafana 12.4 and 13.2; `lab-tested` on the lab ledger |
| Profile-less collector (DC's no-profile queue) | Slice 3: not built | — |
| AWS pricing (USD), home dashboard, alerts | Slice 4: not built | — |
| Helm chart, offline bundle, dry-run review | Slice 5: not built | — |

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

`TTV_DRY_RUN=true` (the default) classifies and plans without writing the ledger. Turn it off only after a reviewed dry run of at least a day (AGENT.md §20.6). To view the dashboards, see [`grafana/README.md`](grafana/README.md).

## Configuration

| File | What it holds |
| --- | --- |
| `.env` ([example](.env.example)) | Runtime settings: endpoints, token files, CA bundles, database, cadences, grace periods |
| `config/policy.yaml` ([example](config/policy.example.yaml)) | Versioned business policy: stage owners, objectives, target clusters, exclusions, serving counters, Run:ai kinds, pools, teams |
| `config/clusters.yaml` ([example](config/clusters.example.yaml)) | One read-only connection per target cluster: kube API, Thanos route, token file, CA bundles, expected cluster ID |

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
  classify.py       server-level rules (pure)
  gpu_rows.py       GPU-level rules 15–22 (pure)
  ledger.py         scan decisions → interval opens/closes (pure)
  store.py          Postgres ledger
  engine.py         one scan: fetch → classify → plan → apply
migrations/         plain SQL schema
config/             policy and cluster examples
grafana/            data sources and dashboards as code
deploy/lab/         lab-only manifests (fake GPUs on a cluster without GPUs)
docs/               discovery findings, probe runbooks, ADRs
tests/              unit, fixture and Postgres tests
```

## Documentation

- [`AGENT.md`](AGENT.md): the binding brief. Section 0 overrides Section 20, which overrides Sections 1–19.
- [`docs/discovery.md`](docs/discovery.md): verified source facts, user decisions, contradictions and open questions.
- [`docs/discovery-probes.md`](docs/discovery-probes.md): read-only probe runbooks for the air-gapped production environment.
- [`docs/adr/`](docs/adr/): architecture and classification decisions.
- [`deploy/lab/README.md`](deploy/lab/README.md): exercising the GPU rules on a cluster without GPUs.

## License

All rights reserved (see `pyproject.toml`). No open-source licence has been chosen.
