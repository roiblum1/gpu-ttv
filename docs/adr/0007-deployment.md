# ADR-0007: Packaging and deployment (image, Helm charts, offline bundle, CI)

- **Status:** accepted 2026-10-31 (R1 slice 5).
  - `fixture-tested`: `tests/test_helm_charts.py`, `tests/test_deploy_packaging.py`, `tests/test_api_decisions.py`.
  - `lab-tested`, on a laptop (podman 6.1, arm64; Helm 4.3.1):
    - the image built with the network off, from a wheelhouse;
    - all three entry points ran with a read-only root filesystem, an arbitrary UID and no network;
    - the engine ran a dry run in the image against the sandbox server-scan (3,008 demo servers);
    - `mirror-images.sh` archived one image with its digest intact;
    - `sbom.sh` produced both SBOMs.
  - `lab-tested` on an OpenShift 4.22 workshop cluster (amd64, no GPUs), 2026-10-31:
    - the image built in the cluster by a Docker-strategy BuildConfig from this repository at a fixed commit, with dependencies from PyPI by `uv.lock` hash (`RUN --mount=type=bind` works under OpenShift's buildah);
    - `helm install`, then `helm upgrade` to a new digest, of `gpu-ttv` with the defaults (dry run), a throwaway Postgres 15, the sandbox server-scan, the ServiceMonitor and all three Grafana sidecar parts;
    - the engine ran under the `restricted-v2` SCC with an assigned UID, applied the migrations on Postgres 15 and passed its probes (ready after the first scan), and the dry run wrote no assets, intervals or milestones;
    - user-workload monitoring scraped `/metrics/` through the chart's NetworkPolicy, and the series reached Thanos;
    - sizes: the Helm release Secret stores about 0.4 MB of the 1 MiB limit, because it holds the dashboards twice (chart files and rendered ConfigMaps); the largest dashboard ConfigMap, Delivery, is 218 KiB as JSON, under client-side apply's 256 KiB annotation limit;
    - not covered: `deploy/gpu-cluster-rbac` (installing it grants cluster permissions, which awaits the user's approval), so no target cluster was read; the collector (no managers in the lab); egress rules (`networkPolicy.egress` was empty).
  - Nothing here has run against production.
- **Context:** AGENT.md §20.8 (air-gapped: internal registry and Python mirror, digest-pinned images, no runtime downloads, GitLab CI, Helm, Argo CD; per-cluster RBAC with no write verbs), §20.9 (layout), §15 (offline release process), §20.6 (dry run before writes), §20.12 (read-only verified, offline install test).
- **Files:** `Containerfile`, `.containerignore`, `deploy/helm/gpu-ttv/`, `deploy/gpu-cluster-rbac/`, `offline/`, `.gitlab-ci.yml`, `docs/dry-run-review.md`.

## Decision

### Image

**One image runs all three entry points.** `ttv-engine` is the default command, and the chart runs `ttv-collector` with the same digest.

**The base images are pinned by multi-arch index digest:** `python:3.12-slim` (3.12.16-slim-trixie) and `uv` 0.13.6 (the build stage only). References carry no tag next to the digest, because skopeo refuses `name:tag@digest` (seen 2026-10-31) and container runtimes differ on it. The tags live in `offline/images.lock`.

**Dependencies come from `uv.lock`'s hashes, not from `uv sync`.**
- The build exports the runtime requirements with their hashes (`uv export --frozen --no-dev`).
- It installs them with `uv pip install --require-hashes --only-binary :all:`, either from `PIP_INDEX_URL` (the internal mirror) or from `offline/wheelhouse/`.
- `uv sync --frozen` downloads the URLs recorded in `uv.lock` (files.pythonhosted.org) whatever index is configured. With `UV_DEFAULT_INDEX` pointing at an unreachable host it still succeeded from PyPI (checked 2026-10-31), so it cannot be redirected to a mirror.
- Wheels only: nothing is compiled, and no build backend is needed offline.

**The project runs from `/app/src`.**
- `store.MIGRATIONS` resolves `migrations/` relative to the source tree. A wheel installed into site-packages would not find it.
- A `.pth` file puts `/app/src` on the path. The console scripts are generated from `[project.scripts]`, so they cannot drift from `pyproject.toml`.
- `migrations/` and `config/` (examples, the GPU instance map and the pricing snapshots) are copied in.

**Runtime hardening:**
- `USER 1001`, numeric so `runAsNonRoot` can be verified.
- Files stay root-owned and world-readable: any UID can run them and none can change them. OpenShift's arbitrary UIDs work.
- `HOME=/tmp` and `PYTHONDONTWRITEBYTECODE=1`, with bytecode precompiled, so the root filesystem can be read-only.
- Nothing is fetched at runtime: prices are offline snapshots (ADR-0003).

**`.containerignore` is an allow-list** (`src`, `migrations`, `config`, `offline`, `pyproject.toml`, `uv.lock`), so `.env`, `CLAUDE.local.md`, `.claude/` and `.git` never reach a build.

### Helm chart `deploy/helm/gpu-ttv`

**The engine Deployment:**
- **1 replica by default.** Replicas share the Postgres advisory lock (ADR-0001), so more are safe.
- **`strategy: Recreate`.** A new pod waits for the old pod's lock, so a rolling update could never become ready.
- **Probes:**
  - `startupProbe` on `/healthz` with a long window (1 h by default). A replica waiting for the lock serves no HTTP yet (`main.py` takes the lock before starting the API), and the startup probe keeps liveness from killing it.
  - `livenessProbe` on `/healthz` and `readinessProbe` on `/readyz`.
- **Service and ServiceMonitor.** The Service serves the API and `/metrics`. The ServiceMonitor is optional and scrapes `/metrics/`, because `/metrics` answers 307 to `/metrics/` (the ASGI mount, seen 2026-10-31).

**The collector Deployment** is rendered only when `collector.managers` lists a manager, and then requires `database.existingSecret`. It has no probes: it serves no HTTP, a crash restarts it, and a stalled one raises "TTV collector manager stale".

**Configuration:**
- The policy, clusters and managers files are ConfigMaps. A checksum annotation restarts the workload that reads them, because both read them only at startup.
- The policy defaults to `config/policy.example.yaml`, and `NOTES.txt` warns when it is used.
- `clusters` entries get `token_file` and `ca_bundle` from the Secret each one names (`tokenSecret`, keys `token` and `ca.crt`), which matches the RBAC chart's token Secret.
- The chart fails at render time when a cluster is not in the policy's `target_clusters`, when the collector has no database, or when the digest is not `sha256:<64 hex>`.

**Secrets are referenced by name only:** `database.existingSecret` (the DSN), `serverscan.tokenSecret`, `clusters[].tokenSecret` and `collector.secretMounts`. They are mounted read-only with mode 0440. The chart contains no Secret object.

**Settings:**
- `TTV_DRY_RUN` comes only from `dryRun`, which defaults to `true` (§20.6) and must be a boolean.
- Other scalars go under `settings`. `values.schema.json` lists the 13 settings the chart leaves open and refuses any other key, including `TTV_DRY_RUN`, so a typo or a bypass fails the render.
- `tests/test_helm_charts.py` checks that the open settings plus the 11 the chart sets itself are exactly `ttv.config.Settings`.

**Pods:**
- `runAsNonRoot`, `RuntimeDefault` seccomp, no privilege escalation, all capabilities dropped, a read-only root filesystem with an emptyDir `/tmp`.
- `automountServiceAccountToken: false`. The pods never call their own cluster's API, so the chart has no RBAC.
- Resources: requests and limits are set. They are provisional: the engine held 72 MB resident after classifying 3,008 sandbox servers.

**Shutdown.** `shareProcessNamespace: true` makes the pause container PID 1.
- As PID 1, Python ignores SIGTERM unless it installs a handler. uvicorn stops serving but re-raises SIGTERM, and that re-raise is ignored, so the scan loop kept running until SIGKILL: 20 s in the lab.
- With an init as PID 1, the engine stopped in 0.3 s.
- The collector and a standby engine install no handler, so the same applies to them.

**NetworkPolicy:**
- Ingress to the engine's port is allowed only from the listed peers (default: user-workload monitoring and the same namespace). The collector accepts no ingress.
- Egress is not restricted by default, because the destinations (server-scan, kube APIs, Thanos routes, managers, Postgres) are site-specific CIDRs. Setting `networkPolicy.egress` restricts it, and DNS is then added.

**Grafana provisioning, for a sidecar-based Grafana:**
- **Opt-in ConfigMaps** with the k8s-sidecar labels `grafana_dashboard`, `grafana_datasource` and `grafana_alert`, rendered from the repo's `grafana/` files. `files/grafana` and `files/policy.example.yaml` are symbolic links into the repo, which Helm follows when it renders and packages the chart, so there is no copy to drift. A test compares every rendered file byte for byte.
- **One ConfigMap per dashboard,** to stay under the 1 MiB object limit. The largest dashboard is 205 KB.
- **Argo CD:** an annotation asks for server-side apply, because client-side apply's last-applied annotation is capped at 256 KiB.
- **The notification policy is optional** (`grafana.alerting.notifications`), because `ttv-notifications.yaml` replaces an organisation's whole policy tree (ADR-0006).

### Cluster RBAC `deploy/gpu-cluster-rbac`

A small chart, installed once per target cluster by its owners (`helm install`, or `helm template | oc apply`).

**The ServiceAccount's ClusterRole:**
- **Verbs** are `get`, `list` and `watch`, fixed in the template; values cannot add others.
- **Built-in kinds:** the approved list (`docs/discovery.md`, 2026-10-28): nodes, pods, namespaces, replicasets, deployments, statefulsets, jobs, and clusterversions for the `cluster_id` check.
- **CRD kinds** (Run:ai, KServe, LeaderWorkerSet, Kubeflow, Knative) come only from `discover-kinds.sh`, which runs `oc api-resources` on that cluster. CRD names are never written by hand (CLAUDE.md), and Run:ai is on some clusters only.
- **Refused resources:** the template refuses secrets, service accounts, OAuth token kinds, subresources (`pods/exec`) and wildcards, the same plurals `sources/kube.py` refuses in code.
- **Discovery URLs:** `get` on the API discovery URLs, which most clusters grant to every user anyway.

**The rest:**
- A binding to `cluster-monitoring-view` for the Thanos querier (D11).
- A `kubernetes.io/service-account-token` Secret with no data: the token controller fills it, and the token does not expire.

### Offline delivery `offline/`

- **`images.lock`** holds every third-party image with its index digest and role: the build bases, the CI tools, skopeo, syft, Grafana 12.4.14 (validated, not deployed by the chart) and Postgres 17.11 (CI and the clean-room test, not deployed). Tests check that every pinned reference in the Containerfile and `.gitlab-ci.yml` is in the lock.
- **`mirror-images.sh`** copies each image with `skopeo copy --all --preserve-digests` into an OCI archive. It hashes the archive's top-level manifest and stops unless the hash equals the pinned digest. All architectures are copied, because the repo pins index digests; the bundle is therefore larger than a single-architecture one.
- **`import-images.sh --registry <host>`** first verifies `SHA256SUMS`, which needs nothing online and no transparency log. It then pushes each archive to `<host>/<path>:<tag>` and checks that `<host>/<path>@<digest>` resolves. Credentials come from a prior login or `REGISTRY_AUTH_FILE`, never from arguments.
- Both scripts use a skopeo binary when there is one. Otherwise they run the pinned skopeo image under podman; the bundle carries that image as `tools/skopeo.tar`.
- **`build-wheelhouse.sh`** runs `pip download --require-hashes --only-binary=:all:` inside the pinned Python image, so markers evaluate as Linux, for a chosen architecture. The wheels seed the internal Python mirror, or `offline/wheelhouse/` for a mirror-less build.
- **`sbom.sh`** writes a CycloneDX SBOM of the Python dependencies (`uv export --format cyclonedx1.5`, always) and an SPDX SBOM of each given image archive with syft (binary, else the pinned image).
- **`make-bundle.sh`** assembles the images, wheelhouse, both packaged charts, a `git archive` of the source, the SBOMs, the scripts, `tools/skopeo.tar` and `SHA256SUMS`. With `--image`, it also adds a gpu-ttv image built on the connected side, records its digest and writes `charts/values-image.yaml`.

### CI

`.gitlab-ci.yml` is for the internal GitLab:
- **`gate`:** ruff, ruff format, ty and pytest with a Postgres service. It uses `helm` copied from the helm image, so the chart tests run there too.
- **`charts`:** lint and template of both charts.
- **`build-image`:** buildah, with the Containerfile's base digests rehomed to `INTERNAL_REGISTRY` so the digests are written once, and dependencies from `PIP_INDEX_URL`. It pushes on the default branch and on tags, and exports `IMAGE_DIGEST`.
- **`sbom`:** syft on the pushed digest.
- **`chart-package`** (tags): the packaged charts and `values-image.yaml`.

Every job image is an internal-registry reference by locked digest. `.github/workflows/ci.yml` (the public repo) is unchanged.

### `GET /decisions`

A read-only endpoint that lists the last scan's buckets per asset, filterable by category and reason. The dry run writes no ledger, so without it the §20.12 spot check of 10 assets across all categories would have no list to sample from (`docs/dry-run-review.md`).

## Consequences

**Install decisions left to the user:**
- the internal registry host;
- the namespace;
- the Postgres the ledger lives in (I4: the chart deploys none);
- how Grafana is run: a sidecar-based Grafana, a direct mount of `grafana/`, or the Grafana Operator, which would need `GrafanaDashboard` objects the chart does not render;
- egress CIDRs;
- whether to expose `/explain` beyond `port-forward`. The API has no authentication, so it needs an authenticating proxy first.

**Things the build and CI depend on:**
- The GitLab runner must be able to run buildah. `vfs` plus `chroot` isolation usually needs a privileged or `anyuid` runner on OpenShift.
- The internal Python mirror's CA must be trusted by the build image.

**Open limits:**
- **Untested installs.** The chart was rendered and linted, never installed. The §15 clean-room install test and the §20.6 dry run remain to be run on site. The import script was tested only against a stub skopeo, never against a registry.
- **The collector has no health endpoint.** A hung collector is caught only by the staleness alert.
- **SIGTERM handling.** A follow-up could add a handler in `main.py`; `shareProcessNamespace` would then be unnecessary.
- **The standby engine serves no HTTP while it waits for the lock.** That is why the startup probe exists. Serving `/healthz` while waiting would let the startup window shrink.
