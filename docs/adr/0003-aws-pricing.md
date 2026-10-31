# ADR-0003: AWS On-Demand comparison pricing in USD

- **Status:** accepted 2026-10-31. The user approved the proxies, the size rule and the unpriced V100 16GB the same day, and asked for B300 and H100 94/95GB (map 2026-10-31.2).
- **Context:** AGENT.md §0.5, §10 "AWS comparison pricing", §17 "Numerical checks", §20.5, §20.7, §20.11; user decision of 2026-10-31 in `docs/discovery.md` ("Inputs from the user").
- **Code:** `src/ttv/pricing/` (`catalog.py`, `mapping.py`, `snapshot.py`, `capture.py`, `importer.py`, `cli.py`), `migrations/0002_pricing.sql`, `src/ttv/queries.py`, `grafana/dashboards/ttv-queues.json` (panels 16-20).

## Decision

Idle and overdue GPU-hours are valued at the **AWS On-Demand** price of a comparable EC2 instance (**Linux, shared tenancy, us-east-1**), shown in **USD**.

```
per_gpu_hour_usd = instance On-Demand price per hour / physical GPUs in the instance
value_usd        = GPU-hours x per_gpu_hour_usd       (computed at query time, never stored)
```

- **USD, not ILS.** The user's decision overrides §0.5's ILS default. The `fx_usd_ils` and `fx_date` columns stay NULL; no exchange rate is needed to display value.
- **us-east-1** resolves open input I3. It is the region whose price list is captured. Changing it means capturing a snapshot for another region; the engine never mixes regions within a historical valuation.

### What the number means (§10)

This is the **AWS-equivalent value** view: a transparent external comparison. It is **not an AWS bill, not our cost and not a proven loss or avoidable spend**. It is never added to energy cost or internal ownership cost.

The per-GPU rate **allocates the whole instance price, including the host's CPUs, memory, storage and network, across its GPUs**. It is not a GPU-only tariff that anyone can buy. Similar GPU names or equal GPU counts do not prove equal performance. AWS's H100, H200 and A100 are SXM parts, for example, while a PCIe card of the same name in our fleet is slower and is still valued at the SXM rate. The value is a **linear capacity equivalent**: 8 GPU-hours of an 8-GPU instance equal one instance-hour, never eight (§17, tested). It does not say how many whole instances a particular workload would need.

### Mapping: canonical GPU model → instance (`config/pricing/gpu-instance-map.yaml`)

- **Canonical names** come from server-scan's GPU catalogue, ported at commit `defe172` (`catalog.py`). Raw model strings are canonicalised by the same rules: upper-case, drop leading vendor words, strip separators, compare for **equality**, never substrings. Two local additions are marked in the code:
  1. a Cisco PID whose last number is its capacity (`UCSC-GPU-A100-40` → `NVIDIA A100 40GB`), accepted only when a catalogue row with that PID base has exactly that VRAM;
  2. a bare family that shipped in several capacities (`NVIDIA A100`) becomes `NVIDIA A100 (memory unknown)`, not a guess.
  Anything else unrecognised (e.g. `NVIDIA RTX A6000`, `""`) has no canonical name and is **unpriced**.
- **`gpu_model_aliases`** records raw → canonical with the catalogue version. The engine upserts the models it sees on every scan, in the same transaction as the intervals, and re-canonicalises every raw model in the ledger at startup. Rows change only when the mapping or the catalogue version changes. A catalogue upgrade can re-map a raw name, and with it the value of past intervals. That is an identity correction, not a price change, and the version is recorded.
- **Kinds.** `exact` means the same GPU model and memory; form-factor differences are stated in `reason`. `proxy` means a different but comparable GPU; every proxy is approved by the user and listed below. `unpriced` means there is no defensible AWS equivalent, with the reason; it is shown as unpriced GPU-hours, never as $0. Every catalogue model has an entry (tested).
- **Size rule.** Use the smallest size with at least 4 GPUs of the model. That matches 4-8 GPU servers and gives each GPU the least host in the split. P families sell one multi-GPU size (8 GPUs). For G families the rule matters because the 8-GPU sizes carry a disproportionately large host. See the alternatives in `docs/discovery.md`, "AWS pricing".
- Mapping facts come from AWS's instance-type specifications (`documented`), cross-checked at capture against the price list's `GPU` column.

**Approved by the user (2026-10-31):**

| Item | Choice | Alternative |
| --- | --- | --- |
| Proxy `NVIDIA A10 24GB` | `g5.12xlarge` (A10G, AWS's variant of the A10) | unpriced |
| Proxy `NVIDIA A100 (memory unknown)` | `p4d.24xlarge` (A100 40GB rate, the lower of the two A100 capacities' rates, so a lower bound) | unpriced; better: teach server-scan the model (`INVENTORY_GPU_MODELS`) |
| Size rule for G families | smallest ≥ 4-GPU size (`g6e.12xlarge`, `g6.12xlarge`, `g5.12xlarge`, `g4dn.12xlarge`, `g7e.24xlarge`) | 8-GPU sizes: L40S +44 %, L4 +45 %, A10 +44 % per GPU-hour; T4 and RTX PRO 6000 unchanged |
| `NVIDIA Tesla V100 16GB` | unpriced: AWS's current specification pages no longer document p3 | `p3.8xlarge` ($3.06/GPU-h in the price list) if the price list alone is accepted as evidence |
| `NVIDIA B300 288GB` (added on request) | exact: `p6-b300.48xlarge`, 8 x B300 (268 GiB = 288 GB each) | — |
| Proxy `NVIDIA H100 NVL 94GB` and `NVIDIA H100 94GB` (inventory "H100 95GB"; added on request) | `p5.48xlarge` (H100 SXM 80GB, the only H100 on EC2) | unpriced |

### Snapshots: immutable, dated, offline

- A snapshot is one JSON file (`config/pricing/snapshots/<snapshot_id>.json`): provider, service, region, currency, purchase option, OS, tenancy, billing unit, the AWS **Publication Date** and offer version, capture time, source URL, **sha256 of the raw price list**, parser, map and catalogue versions, the exact row filters, and per model: instance type, SKU, RateCode, instance price, GPU count, per-GPU rate, kind, confidence, reason, the price term's EffectiveDate, and AWS's GPU memory, vCPU and memory columns. Unpriced models are listed with their reasons.
- **`snapshot_id` = `aws-ec2-ondemand-<region>-<publication>-map-<map version>`**. One AWS publication priced with one map version is one id. `content_sha256` covers the canonical JSON (sorted keys, compact separators) of every other field. Loading verifies it, so **a changed byte is refused**, and it re-checks `per_gpu_hour_usd = instance_hourly_usd / instance_gpus`.
- **Capture (connected side only):** `ttv-pricing capture --region us-east-1 --out config/pricing/snapshots/` streams the official bulk CSV `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/<region>/index.csv` (about 300 MB) with httpx. It hashes the raw bytes as they arrive and parses line by line with `csv`. It keeps a row only when every one of these holds: `TermType=OnDemand`, `MarketOption=OnDemand`, `Operating System=Linux`, `Tenancy=Shared`, `Pre Installed S/W=NA`, `CapacityStatus=Used`, `License Model=No License required`, `Unit=Hrs`, `Product Family=Compute Instance`, `Location Type=AWS Region`, `Currency=USD`, `operation=RunInstances`, `Region Code=<region>`. **Exactly one positive price per mapped instance type, with the map's GPU count, or the capture fails** and lists every problem. `--allow-missing` records a missing offer as unpriced with that reason; it never substitutes another term (§10). `--keep-raw` saves the raw file as evidence; `--from-file` re-parses a saved one.
- **The engine never fetches prices.** At startup it verifies and imports every file in `TTV_PRICING_DIR`, idempotently. It refuses to start if a file is tampered with, or if an id is already imported with different content (ids are immutable). The CLI's `ttv-pricing import <file|dir>` does the same for an existing database. `ttv-pricing verify` and `list` check files and imports.
- **Air-gapped update:** capture on the connected side → review the per-GPU table it prints → commit or carry the JSON file → place it in `TTV_PRICING_DIR` (or run `ttv-pricing import`) → restart. **Rollback:** set `TTV_PRICING_SNAPSHOT` to an older id and restart. Nothing is deleted, and historical valuation is unaffected.

### Present-day and historical valuation

- **Present-day:** every GPU-hour at one selected snapshot. That is `TTV_PRICING_SNAPSHOT`, else the latest by publication date (then capture time). The engine records its choice in `pricing_selection`, and Grafana's `snapshot` variable defaults to it.
- **Historical:** each interval at the latest snapshot of the same provider, region, purchase option and currency **published at or before the interval's start**. No such snapshot means unpriced. **A newer snapshot never changes historical value** (tested). An interval that spans a publication is valued wholly at the snapshot in force at its start. Intervals are short (a scan changes them), so this is a small approximation; it is documented, not hidden.
- **Unpriced is never zero.** Every value query returns unpriced GPU-hours beside the USD figure. A row with nothing priced has no value (NULL), not $0. The engine also exports `ttv_unpriced_gpus{gpu_model}`: GPUs of any category in the last scan whose canonical model has no rate in the present-day snapshot, labelled with the raw model.

## Consequences

- `src/ttv/classify.py` and the ledger are unchanged; intervals still hold physical GPU counts only.
- A new GPU model in the fleet shows up as unpriced (Grafana "Unpriced idle GPUs now, by model") until the catalogue and the map cover it and a new snapshot is captured.
- Not built: Spot, Savings Plans, Reserved Instances and Capacity Blocks (opt-in per §10); value of productive GPU-hours; the home dashboard's KPI card (slice 4 Home page) reuses `IDLE_VALUE_PER_HOUR_NOW`.
