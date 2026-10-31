# Read-only access on a target GPU cluster

One release per target cluster gives gpu-ttv what AGENT.md §20.8 allows and nothing more:
- a ServiceAccount;
- a ClusterRole with **get, list and watch only**, on the kinds the engine reads;
- a binding to `cluster-monitoring-view`, for the Thanos querier;
- a long-lived token Secret.

The verbs are fixed in the template, so values cannot add a write verb. The chart refuses secrets, service accounts, token kinds, subresources such as `pods/exec`, and wildcards.

**What the ClusterRole covers:**
- **Always:** nodes, pods and namespaces; replicasets, deployments and statefulsets; jobs; and clusterversions, for the `cluster_id` check. This is the list approved on 2026-10-28 (`docs/discovery.md`).
- **When the cluster has them:** the Run:ai, KServe, LeaderWorkerSet, Kubeflow and Knative kinds that can own GPU pods, so the engine can walk `ownerReferences` to the workload.

CRD names differ by version and by cluster, and Run:ai is installed on some target clusters only. So they are discovered on each cluster, never typed by hand.

Evidence: `fixture-tested` only (`tests/test_helm_charts.py`, `tests/test_deploy_packaging.py`). It has not been installed on a cluster.

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

That Secret's `ca.crt` is the cluster's service CA bundle. If the API's external name uses a certificate from another CA, put that CA in `ca.crt` instead.

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

```bash
SA=system:serviceaccount:gpu-ttv-reader:gpu-ttv-reader
for verb in create update patch delete; do oc auth can-i $verb pods --as=$SA; done   # "no" four times
oc auth can-i get secrets --as=$SA                                                     # "no"
oc auth can-i list nodes --as=$SA                                                      # "yes"
```

Paste the output into the dry-run record (`docs/dry-run-review.md`).
