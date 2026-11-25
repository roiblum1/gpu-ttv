# Read-only access on a target GPU cluster

One release per target cluster gives gpu-ttv what AGENT.md §20.8 allows and nothing more:
- a ServiceAccount;
- a ClusterRole with **get, list and watch only**, on the kinds the engine reads, plus **get** on `pods/proxy` to read vLLM's `/metrics` (`podMetrics.enabled`, below);
- a binding to `cluster-monitoring-view`, for the Thanos querier;
- a long-lived token Secret.

The verbs are fixed in the template, so values cannot add a write verb. The chart refuses secrets, service accounts, token kinds, subresources such as `pods/exec`, and wildcards in values. `pods/proxy` is the only subresource, and the template fixes it to `get`.

**What the ClusterRole covers:**
- **Always:** nodes, pods and namespaces; replicasets, deployments and statefulsets; jobs; and clusterversions, for the `cluster_id` check. This is the list approved on 2026-10-28 (`docs/discovery.md`).
- **When the cluster has them:** the Run:ai, KServe, LeaderWorkerSet, Kubeflow and Knative kinds that can own GPU pods, so the engine can walk `ownerReferences` to the workload.
- **Unless `podMetrics.enabled` is false:** `get` on `pods/proxy`, for serving requests (next section).

CRD names differ by version and by cluster, and Run:ai is installed on some target clusters only. So they are discovered on each cluster, never typed by hand.

Evidence: `fixture-tested` (`tests/test_helm_charts.py`, `tests/test_deploy_packaging.py`) and `lab-tested` on an OpenShift 4.22 workshop cluster (2026-10-31). There gpu-ttv read nodes, pods, owner chains up to KServe's LLMInferenceService, and Thanos, with this chart's token, and the checks below gave the expected output (ADR-0007). That lab run used chart 0.1.0; the `pods/proxy` rule added in 0.2.0 is `fixture-tested` only.

## Reading vLLM's `/metrics` (`podMetrics.enabled`)

The engine decides whether a served model is used from vLLM's request counter, `vllm:request_success_total`. It reads that counter from each vLLM pod's own `/metrics` page, through the API server's pod proxy, with this chart's token (ADR-0009):

```
GET /api/v1/namespaces/<namespace>/pods/http:<pod>:<port>/proxy/metrics
```

This works even when nobody created a ServiceMonitor for the model. The engine sends only `GET`, only to `/proxy/metrics`, with no query string: `http` first, then `https` once if the pod serves `/metrics` over TLS. Its request guard refuses anything else before it is sent (`src/ttv/sources/pod_metrics.py`).

**The trade-off.** Kubernetes cannot narrow this permission to one path or port. `get` on `pods/proxy` lets the token send a GET to any port and path of any pod in the cluster, through the API server. Some security teams review that before they approve it. OpenShift's `Default` audit profile records each request's metadata under this ServiceAccount.

**Without it** (`--set podMetrics.enabled=false`), the API server answers 403. The engine logs `pod_metrics.permission_denied`, and serving models show "No data" (UNKNOWN) on the dashboards, never zero requests. GPU counts, Run:ai and DCGM utilisation are unaffected.

## Install (the cluster's platform owners)

```bash
export KUBECONFIG=<target cluster>
deploy/gpu-cluster-rbac/discover-kinds.sh > discovered-<cluster>.yaml   # runs only `oc api-resources`
# review discovered-<cluster>.yaml, then either
helm install gpu-ttv-reader deploy/gpu-cluster-rbac -n gpu-ttv-reader --create-namespace -f discovered-<cluster>.yaml
# or, without Helm on that cluster
helm template gpu-ttv-reader deploy/gpu-cluster-rbac -n gpu-ttv-reader -f discovered-<cluster>.yaml > reader.yaml
oc create namespace gpu-ttv-reader && oc apply -n gpu-ttv-reader -f reader.yaml
```

`discover-kinds.sh` takes other API groups as arguments, for example `ray.io`. Re-run it after a Run:ai or KServe upgrade and upgrade the release. Until a kind is listed, the engine cannot look up owners of that kind, and their GPUs show as `HOLDER_UNRESOLVED` (UNKNOWN), never as idle.

## Hand over the token

The token controller fills `gpu-ttv-reader-token`. Copy its token, and the CA that signs the API server's certificate, into a Secret in the gpu-ttv namespace on the cluster where gpu-ttv runs:

```bash
oc -n gpu-ttv-reader get secret gpu-ttv-reader-token -o jsonpath='{.data.token}' | base64 -d > token
oc -n gpu-ttv-reader get secret gpu-ttv-reader-token -o jsonpath='{.data.ca\.crt}' | base64 -d > ca.crt
# on the gpu-ttv cluster:
oc -n gpu-ttv create secret generic ttv-reader-<cluster> --from-file=token --from-file=ca.crt
shred -u token ca.crt
```

On OpenShift 4.22 that Secret's `ca.crt` holds the kube-apiserver signers, including `kube-apiserver-lb-signer`, which signs the external `api.` name, so it verifies `api_url` as is (lab, 2026-10-31). Replace it only if the API serves a custom certificate from another CA.

Then add the cluster to the gpu-ttv values:

```yaml
clusters:
  - name: <cluster>                 # as in server-scan's openshift.cluster_name, and in policy.target_clusters
    api_url: https://api.<cluster domain>:6443
    thanos_url: https://thanos-querier-openshift-monitoring.apps.<cluster domain>
    cluster_id: <oc get clusterversion version -o jsonpath='{.spec.clusterID}'>
    tokenSecret: ttv-reader-<cluster>
```

The engine reads the token file again on every scan. To rotate the token, delete `gpu-ttv-reader-token`, re-apply the release so the controller issues a new one, and update the copy.

## Verify read-only (§20.12)

Compare the reader with the namespace's `default` ServiceAccount, which has no bindings, so only what this chart grants is left:

```bash
SA=system:serviceaccount:gpu-ttv-reader:gpu-ttv-reader
BASE=system:serviceaccount:gpu-ttv-reader:default
diff <(oc auth can-i --list -n gpu-ttv-reader --as=$BASE | sort -u) \
     <(oc auth can-i --list -n gpu-ttv-reader --as=$SA | sort -u) | grep '^[<>]'
for verb in create update patch delete; do oc auth can-i $verb pods --as=$SA; done   # "no" four times
oc auth can-i get secrets --as=$SA                                                     # "no"
oc auth can-i get pods/proxy --as=$SA                                                  # "yes" unless podMetrics.enabled=false
```

The diff shows only `>` lines: the kinds above with `[get list watch]`, `pods/proxy` with `[get]` (unless `podMetrics.enabled` is false), and `prometheuses.monitoring.coreos.com/api [k8s] [get create update]`. The `prometheuses` line is OpenShift's `cluster-monitoring-view`, which authorizes Thanos queries and cannot change the Prometheus object (`docs/dry-run-review.md`, step 2). Paste the output into the dry-run record.
