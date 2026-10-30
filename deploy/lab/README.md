# Fake-GPU lab (lab clusters only)

Exercises the GPU rows of ADR-0002 (rows 14–22) on an OpenShift cluster that has no GPUs. Evidence from it is `lab-tested`, never `production-verified`.

What it creates:
- **8 fake GPUs on one worker.** A node status patch advertises `nvidia.com/gpu: 8`, using Kubernetes' extended-resource mechanism. No device plugin and no real devices: pods requesting the resource are scheduled and run normally.
- **A fake DCGM exporter** (`ttv-lab/fake-dcgm-exporter`) reporting 7 of the 8 GPUs with the real exporter's labels, so one GPU reads as missing.
- **GPU holders:**
  - `chat-busy` (2 GPUs) and `chat-idle` (1 GPU): fake vLLM servers exposing `vllm:request_success_total`.
  - `chat-unmonitored` (1 GPU): the same server with no metrics scrape.
  - `notebook` (1 GPU): sleeps.
  - `crashloop` (1 GPU): exits.
- **ServiceMonitors** for the real `qwen-tiny` vLLM and its llm-d EPP. The EPP's `/metrics` needs a token, which comes from the `ttv-lab-metrics-reader` service account.

Expected classification of the worker (8 GPUs in inventory, A = 8, R = 7), once the scrapes have run for a minute and `warmup_grace` has passed:

| GPUs | Row | Why |
| --- | --- | --- |
| 1 | `GPU_MISSING` (POPAI) | DCGM reports 7 of 8 |
| 2 | `SERVING_IN_USE` (productive) | after requests are sent to `chat-busy` |
| 1 | `NO_REAL_DEMAND` (Serving) | `chat-idle` gets no requests |
| 1 | `TRAFFIC_UNOBSERVED` (UNKNOWN) | `chat-unmonitored` has no counter series |
| 1 | `OTHER_WORKLOAD` (Serving) | `notebook` |
| 1 | `WORKLOAD_NOT_READY` (Serving) | `crashloop` |
| 1 | `NO_WORKLOAD` (Serving) | nothing holds it |

## Apply (run these yourself; Claude's hook blocks cluster writes)

```bash
export KUBECONFIG=<lab kubeconfig>
oc patch node worker-cluster-64spr-1 --subresource=status --type=json \
  -p '[{"op":"add","path":"/status/capacity/nvidia.com~1gpu","value":"8"}]'
oc apply -f deploy/lab/fake-gpu-lab.yaml
```

The engine matches the worker to inventory through a synthetic server-scan record named after the node. The lab runner adds that record, because the sandbox server-scan is read-only.

## Remove

```bash
oc delete -f deploy/lab/fake-gpu-lab.yaml
oc patch node worker-cluster-64spr-1 --subresource=status --type=json \
  -p '[{"op":"remove","path":"/status/capacity/nvidia.com~1gpu"}]'
```

The kubelet also drops a manually advertised extended resource when it re-registers, for example after a node reboot.
