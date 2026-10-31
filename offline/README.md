# Offline delivery and air-gapped install

How a release of gpu-ttv gets from a connected machine to the air-gapped production network and is installed there (AGENT.md §15, §20.8). The design is in [ADR-0007](../docs/adr/0007-deployment.md).

Evidence, on 2026-10-31:
- `lab-tested` on a laptop (podman 6.1, arm64):
  - the wheelhouse built;
  - the image built from it with `--network none`;
  - all three entry points ran with no network, a read-only root filesystem and an arbitrary UID;
  - one image was mirrored with its digest verified;
  - both SBOMs were generated.
- `fixture-tested`: the import script, against a stub skopeo.

Not yet run: a full bundle, an import into a real registry, or the clean-room install on site.

## What is where

| File | Side | Purpose |
| --- | --- | --- |
| `images.lock` | both | Every third-party image, pinned by multi-arch index digest, with its role. The gpu-ttv image is added per release |
| `make-bundle.sh` | connected | Assembles the whole bundle and `SHA256SUMS` |
| `mirror-images.sh` | connected | Archives each locked image (all architectures) and checks the archive's digest equals the pinned one |
| `build-wheelhouse.sh` | connected | Hash-checked wheels for every package in `uv.lock`, for the internal Python mirror |
| `sbom.sh` | either | CycloneDX SBOM of the Python dependencies; SPDX SBOM of image archives (syft, else its pinned image) |
| `import-images.sh` | air-gapped | Verifies `SHA256SUMS`, then pushes each archive to the internal registry with the same path, tag and digest |
| `lib.sh` | both | Shared helpers |

The registry host, the Python mirror URL and the namespace are parameters. The values used below (`registry.internal.example`, `pypi.internal.example`, `gpu-ttv`) are placeholders.

## 1. Connected side: build the bundle

Prerequisites: `podman`, `uv`, `helm` and `git`. A `skopeo` binary is optional; without one, the pinned skopeo image runs under podman.

```bash
offline/make-bundle.sh --out /media/transfer/gpu-ttv-$(git rev-parse --short HEAD) --arch x86_64
# optional: also carry an image built here instead of building it in internal GitLab CI
podman build -t localhost/gpu-ttv:release . && offline/make-bundle.sh --out ... --image localhost/gpu-ttv:release
```

The bundle holds:

```
images/*.tar, images/index.txt    third-party images (OCI archives, digests preserved) [+ gpu-ttv.tar]
wheelhouse/                       wheels + requirements.txt (hashes) + its own SHA256SUMS
charts/                           gpu-ttv-<v>.tgz, gpu-ttv-cluster-reader-<v>.tgz [+ values-image.yaml]
source/gpu-ttv-<rev>.tar.gz       the source tree (git archive)
sbom/                             gpu-ttv-python.cdx.json [+ gpu-ttv.spdx.json]
tools/skopeo.tar                  skopeo for an import host without it
offline/, images.lock, README.md  these scripts and this page
SHA256SUMS                        every file above
```

Record the `SHA256SUMS` file's own hash, `sha256sum SHA256SUMS`, through a separate channel such as the change ticket. The import side then checks the whole bundle against it without anything online.

## 2. Air-gapped side: verify and import

```bash
cd /media/transfer/gpu-ttv-<rev>
sha256sum SHA256SUMS                    # compare with the hash recorded on the connected side
sha256sum -c --quiet SHA256SUMS         # every file intact (import-images.sh checks this again)

# Images: log in first (skopeo login or podman login), then import. Without a skopeo binary:
#   podman load -i tools/skopeo.tar && export SKOPEO_IMAGE=localhost/gpu-ttv-tools/skopeo:latest
offline/import-images.sh --registry registry.internal.example --bundle .
```

**Python mirror.** Upload `wheelhouse/*.whl` to the internal PyPI repository (Artifactory, Nexus or devpi). Every file's hash is in `wheelhouse/requirements.txt`, and the build refuses any other file. To build without a mirror, put the wheelhouse at `offline/wheelhouse/` in the source tree; the Containerfile then installs from it with no index.

**Source.** Push the source to the internal GitLab, and set the CI/CD variables `INTERNAL_REGISTRY`, `PIP_INDEX_URL`, `REGISTRY_USER` and `REGISTRY_PASSWORD` (masked). The pipeline in `.gitlab-ci.yml` then:
- runs the gate;
- builds the image from the internal mirrors;
- pushes it on the default branch and on tags;
- writes the SBOM;
- on tags, packages the charts with `values-image.yaml` holding the pushed digest.

**Runtime prerequisites:**
- internal DNS for every endpoint;
- time sync (NTP) on the nodes;
- the internal CA as a ConfigMap (`caBundle.configMap`);
- the Postgres database (I4: the chart deploys none);
- a Grafana that can read it with a `SELECT`-only role (`grafana/README.md`).

## 3. Read-only access on each target cluster

The platform owners of each target cluster run this. It creates no workload. See `deploy/gpu-cluster-rbac/README.md`.

```bash
KUBECONFIG=<target> deploy/gpu-cluster-rbac/discover-kinds.sh > discovered-<cluster>.yaml   # read-only
helm install gpu-ttv-reader charts/gpu-ttv-cluster-reader-<v>.tgz -n gpu-ttv-reader --create-namespace \
  -f discovered-<cluster>.yaml
```

Then hand the token and CA over as a Secret in the gpu-ttv namespace; the commands are in that README.

## 4. Secrets and values

Create these Secrets yourself, from files, so no value lands in shell history or in this repository:

```bash
oc -n gpu-ttv create secret generic ttv-database --from-file=url=./dsn               # postgresql://...?sslmode=verify-full
oc -n gpu-ttv create secret generic ttv-serverscan --from-file=token=./auditor-token
oc -n gpu-ttv create secret generic ttv-ucs-central --from-file=password=./ucs-password
oc -n gpu-ttv create configmap ttv-ca-bundle --from-file=ca-bundle.pem=./internal-ca.pem
```

Write `values-production.yaml` from `deploy/helm/gpu-ttv/values.yaml`:
- `image.registry` and `image.digest`;
- `serverscan`, `database`, `caBundle`;
- the real `policy`, `clusters` and `collector.managers`;
- leave `dryRun: true`.

## 5. Install, dry run, then writes

```bash
helm upgrade --install gpu-ttv charts/gpu-ttv-<v>.tgz -n gpu-ttv -f charts/values-image.yaml -f values-production.yaml
```

With Argo CD, point an Application at the chart with the same values files.

Then run the at-least-one-day dry run and its review in [`docs/dry-run-review.md`](../docs/dry-run-review.md). Writes are enabled only by changing `dryRun` to `false` after a go.

## Upgrade and rollback

**Upgrade.**
1. Import the new bundle.
2. Take a Postgres backup.
3. Set the new `image.digest` and run `helm upgrade`. The engine restarts (`Recreate`) and applies new migrations at start.

**Rollback.**
- **Without a migration in between:** `helm rollback gpu-ttv <revision>`, or set the previous digest.
- **Across a migration:** migrations are plain forward-only SQL, so restore the backup taken before the upgrade, then roll back.
- **Prices:** select an older snapshot with `settings.TTV_PRICING_SNAPSHOT` (ADR-0003).

**Policy and price changes need no new image:**
- Change `policy`, bumping its `policy_version`.
- Or give `pricing.snapshotsConfigMap` a ConfigMap of snapshot files from `ttv-pricing capture`, made on the connected side and carried in like the bundle.

## Clean-room install test (§15, §20.12)

To pass, run it in a namespace on a cluster with public egress denied and no image cache:
1. Import and install from the bundle alone.
2. Check that every pod pulls from the internal registry by digest.
3. Check that the engine reaches `/readyz` 200 in dry run.
4. Check that Grafana renders the pages with no external request (the browser's network tab).
5. Restart every pod and check that it recovers.
6. Upgrade to the next bundle and roll back.

Record the result next to the dry-run record.

## Network flows

| Source | Destination | Port | Purpose | Authentication |
| --- | --- | --- | --- | --- |
| ttv-engine | server-scan | 443/TCP | inventory, GET only | AUDITOR bearer token |
| ttv-engine | each target cluster's kube API | 6443/TCP | nodes, pods, owners; GET only | reader ServiceAccount token |
| ttv-engine | each target cluster's Thanos querier route | 443/TCP | instant PromQL queries | the same token (`cluster-monitoring-view`) |
| ttv-engine, ttv-collector | Postgres | 5432/TCP | the ledger and collector reads | password from the DSN Secret; TLS `verify-full` recommended |
| ttv-collector | UCS Central, and UCS Manager domains for the fallback | 443/TCP | GPU servers without a profile; read-only queries | the account and password file per manager |
| ttv-collector | OpenManage Enterprise; iDRAC for the Redfish fallback | 443/TCP | the same | the same |
| user-workload Prometheus | ttv-engine | 8080/TCP | `/metrics/` | none (in-cluster, NetworkPolicy) |
| Grafana | Postgres | 5432/TCP | dashboards and alert queries | `SELECT`-only role |
| operator (port-forward) | ttv-engine | 8080/TCP | `/readyz`, `/decisions`, `/explain` | cluster RBAC for port-forward |
| all pods | cluster DNS | 53, 5353 | name resolution | none |

Nothing connects to the internet at runtime. Prices come from offline snapshots, and Grafana uses no external fonts, CDNs or plugins.
