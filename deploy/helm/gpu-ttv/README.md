# gpu-ttv Helm chart

Deploys ttv-engine (scan loop, read API, metrics) and, when managers are configured, the profile-less collector. Design and evidence are in [ADR-0007](../../../docs/adr/0007-deployment.md); the air-gapped install end to end is in [`offline/README.md`](../../../offline/README.md).

**Dry run by default.** `dryRun: true` renders `TTV_DRY_RUN=true`: the engine classifies and logs every scan and writes no ledger rows. Change it only after the review in [`docs/dry-run-review.md`](../../../docs/dry-run-review.md).

## What it renders

| Object | When | Notes |
| --- | --- | --- |
| Deployment + Service `<release>-engine` | always | 1 replica (more wait behind the leader lock), `Recreate`, startup/liveness `/healthz`, readiness `/readyz`, port 8080 |
| ServiceMonitor | `serviceMonitor.enabled` | scrapes `/metrics/` |
| Deployment `<release>-collector` | `collector.managers` lists a manager | requires `database.existingSecret` |
| ConfigMaps `-policy`, `-clusters`, `-managers` | policy always; the others when set | a change restarts the workload that reads it |
| NetworkPolicies | `networkPolicy.enabled` (default) | engine port from the listed peers; collector no ingress; egress only if `networkPolicy.egress` is set |
| ServiceAccount | `serviceAccount.create` | no token mounted, no RBAC bound |
| Grafana ConfigMaps | `grafana.dashboards/datasource/alerting.enabled` | sidecar labels; rendered from the repo's `grafana/` through `files/grafana` (a link, no copy) |

The chart contains no Secret: every credential is an existing Secret you create, named in values.

## Values that matter

| Value | Meaning |
| --- | --- |
| `image.registry`, `image.digest` | internal registry and release digest (`sha256:…` only; the zero placeholder cannot be pulled) |
| `dryRun` | `TTV_DRY_RUN`; boolean; true until the dry-run review signs off |
| `serverscan.url`, `serverscan.tokenSecret` | server-scan URL and the Secret holding the AUDITOR token |
| `database.existingSecret` / `urlKey` | Secret holding the ledger DSN (`TTV_DATABASE_URL`) |
| `caBundle.configMap` / `key` | internal CA bundle, mounted at `/etc/pki/ttv` |
| `policy` | the business policy (schema: `config/policy.example.yaml`); empty uses the example |
| `clusters[]` | `config/clusters.yaml` entries plus `tokenSecret` (keys `token`, `ca.crt`); names must be policy targets |
| `collector.managers`, `collector.secretMounts` | `config/managers.yaml` content and the Secrets its password files come from |
| `settings` | other `TTV_*` scalars; `values.schema.json` lists the allowed names (see `.env.example`) |
| `pricing.snapshotsConfigMap` | price snapshots to import instead of the image's (`TTV_PRICING_DIR`) |
| `grafana.*` | sidecar provisioning; set `grafana.alerting.notifications: false` in a shared Grafana (ADR-0006) |

The Grafana container itself needs `TTV_PG_HOST`, `TTV_PG_DB`, `TTV_PG_USER`, `TTV_PG_PASSWORD`, `TTV_PG_SSLMODE` and `TTV_PG_SSLROOTCERT` for the provisioned datasource (`grafana/README.md`, "Install").

## Check before installing

```bash
helm lint deploy/helm/gpu-ttv -f values-production.yaml
helm template gpu-ttv deploy/helm/gpu-ttv -n gpu-ttv -f values-production.yaml > rendered.yaml   # review it
```

`deploy/helm/gpu-ttv/ci/full-values.yaml` switches every optional part on with synthetic names; `tests/test_helm_charts.py` renders it and the defaults and checks: no RBAC write verb, every image by digest, `TTV_DRY_RUN` true by default, no Secret data, Grafana files byte-identical to `grafana/`, every `TTV_*` variable a real setting, and generated config files that load in the engine and collector.

Installing is the user's step (`helm upgrade --install`, or an Argo CD Application with these values); nothing in this repository installs into a cluster.
