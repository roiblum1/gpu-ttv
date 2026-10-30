# Discovery probes (run inside the air-gapped network)

Read-only probes that answer the open items in `docs/discovery.md`. Run them from a bastion inside the network, sanitise with the redaction step of each part, and bring the sanitised output folders back (the user confirmed on 2026-10-28 that sanitised files may leave the network).

Rules for every probe: read-only (GET, `get`, `list`, `api-resources`, `explain`, `whoami`); logging in and out of a manager is allowed. One exception, in Part B: B1 and B2 create a temporary Job in server-scan's namespace that runs server-scan's own collector in `--dry-run` mode (it reads managers and writes nothing) and delete it afterwards. Nothing else is created, changed or deleted. Never call server-scan's `GET /api/v1/servers/available` (it writes).

## Part A: server-scan

- **Rules:** GET only, with the AUDITOR token. Never call `/servers/available`, which is a GET that writes. About 20 API requests in total, at most 8 of them detail GETs. Paste back only `redacted/P-SS-*`.
- **Needs:** `bash`, `curl`, `jq` ≥ 1.6 and `perl`; P-SS-1 also needs `oc`. **Lab-tested 2026-10-30** against the sandbox server-scan and its cluster (real `oc`; auth off and a fake collector there, so the name-pattern env could not be exercised).

```bash
export SS_URL='https://server-scan.<internal-domain>'   # server-scan Route, no trailing slash
export SS_TOKEN_FILE="$HOME/.ss-auditor-token"           # AUDITOR token (INVENTORY_API_TOKEN_AUDITOR), chmod 600
export OUT="$HOME/ttv-discovery/serverscan"; mkdir -p "$OUT"
export SS_NS='server-scan'                               # namespace of the server-scan release (P-SS-1 only)
mkdir -p ~/.ttv && chmod 700 ~/.ttv; [ -f ~/.ttv/salt ] || (umask 077; head -c 32 /dev/urandom | base64 > ~/.ttv/salt)   # same salt as Part C
```

**P-SS-1: effective name patterns, and upper-case `OCP` names.** Answers open questions 1, 5 and 7, and §20.10 "configured name patterns".
- `oc get` here is a read-only call on the management cluster hosting server-scan. It reads CronJob and Deployment specs and allow-listed ConfigMap keys, and no Secrets.
- If the release was installed with the Helm CLI, this alternative reads release Secrets: `helm -n "$SS_NS" get values <release> -a -o json | jq '{np:.collectors.namePattern, per:(.collectors|with_entries(select(.value|type=="object"))|map_values(.namePattern)), auth:.auth.enabled, ret:.collectors.auditRetention}'`.
```bash
curl -s --max-time 120 -H "Authorization: Bearer $(cat $SS_TOKEN_FILE)" "$SS_URL/api/v1/servers/rows" > "$OUT/raw-rows.json"   # reused by P-SS-2/3/6
oc -n "$SS_NS" get cronjob,deploy -o json | jq '[.items[] | (.spec.jobTemplate.spec.template.spec // .spec.template.spec) as $p |
  {kind, object:.metadata.name, schedule:.spec.schedule, suspend:.spec.suspend, images:[$p.containers[].image],
   env:([$p.containers[].env[]? | select(.name|test("NAME_PATTERN|AUTH_ENABLED|AUDIT_RETENTION|STALE_AFTER|PRUNE_ENABLED"))
         | {(.name): (if has("value") then .value else "valueFrom" end)}] | add)}]' > "$OUT/P-SS-1-config.json"
oc -n "$SS_NS" get cm -o json | jq '[.items[] | {cm:.metadata.name, data:((.data // {}) | with_entries(select(.key|test("NAME_PATTERN|AUTH_ENABLED|AUDIT_RETENTION"))))} | select(.data != {})]' > "$OUT/P-SS-1-configmaps.json"
jq '.items | group_by(.source_provider) | map({(.[0].source_provider // "null"): {
      ocp: map(select(.name|test("^ocp"))) | length,
      OCP_other_case: map(select((.name|test("^ocp")|not) and (.name|test("^ocp";"i")))) | length,
      other: map(select(.name|test("^ocp";"i")|not)) | length}}) | add' "$OUT/raw-rows.json" > "$OUT/P-SS-1-name-prefix.json"
```
Writes `P-SS-1-{config,configmaps,name-prefix}.json`. Look for:
- `INVENTORY_COLLECTOR_NAME_PATTERN`; the shipped default is `^ocp`. Each `INVENTORY_<TYPE>_NAME_PATTERN`: absent means it inherits, `""` means that collector takes everything.
- The Redfish collector filters only through its own pattern; note its `suspend` value. Also `AUTH_ENABLED` and `AUDIT_RETENTION_*`.
- `OCP_other_case > 0`: the match is case-sensitive `re.search`.

**P-SS-2: fleet shape.** Answers open question 6 (exact cluster and MCE strings) and gives the classification baseline.
```bash
jq '.items as $i | def cnt(f): $i | group_by(f) | map({(.[0] | f | tostring): length}) | add;
  {generated_at, total: ($i|length), by_state: cnt(.openshift_state), by_source: cnt(.source_provider), by_vendor: cnt(.vendor),
   by_health: cnt(.health), state_x_source: cnt("\(.openshift_state)/\(.source_provider)"),
   maintenance: ($i|map(select(.maintenance.enabled))|length), unreachable: ($i|map(select(.reachable==false))|length),
   stale: ($i|map(select(.stale))|length), contested: ($i|map(select(.contested_with))|length),
   no_profile_template: ($i|map(select(.profile_template_name==null))|length),
   cluster_names: ($i|map(select(.cluster_name))|group_by(.cluster_name)|map({(.[0].cluster_name): length})|add),
   mce_names: ($i|map(select(.mce_name))|group_by(.mce_name)|map({(.[0].mce_name): length})|add)}' "$OUT/raw-rows.json" > "$OUT/P-SS-2-fleet.json"
```
Writes `P-SS-2-fleet.json`. Cluster and MCE names are kept unhashed on purpose. Look for the exact `ocp4-prod-gpu-site*` strings, the `INSTALLED_TO_INVENTORY` count, and the unreachable, stale and contested counts.

**P-SS-3: one trimmed detail per lifecycle state, plus REDFISH_STANDALONE.** Answers §20.10 "a sample per lifecycle_state" and open question 8.
- At most 2 GETs per selection.
- `GPU_RE` only orders the candidates, GPU-heavy models first. If `gpu_count` comes back 0, put a GPU model you know into `GPU_RE` and rerun.
```bash
GPU_RE='DGX|HGX|XE[0-9]|C240|C480|C845|R7[56]0xa|SYS-|AS-|ESC|GPU'
TRIM='{id,name,model,identity:(.identity|{vendor,serial,serial_normalized,system_uuid}),site_id,source_provider,
 hardware:{gpu_count:(.hardware.gpus|length),gpus:[.hardware.gpus[]|{vendor,model,serial,health,power_watts}]},
 profile_template:(.profile_template|{name,external_id}),
 openshift:(.openshift|{lifecycle_state,cluster_name,mce_name,reported_name,contested_with,claim_changed_at}),
 maintenance:(.maintenance|{enabled,reason}),
 health:{overall:.health.overall,critical_policies:[.health.reasons[]?|select(.severity=="CRITICAL")|.policy_key]},
 reachable,unreachable_since,unreachable_reason,last_seen_at,created_at,stale,unread_fields}'
for sel in 'openshift_state=="AVAILABLE"' 'openshift_state=="INSTALLED"' 'openshift_state=="INSTALLED_TO_INVENTORY"' 'source_provider=="REDFISH_STANDALONE"'; do
  tag=$(echo "$sel" | sed 's/.*=="\(.*\)"/\1/'); f="$OUT/P-SS-3-$tag.json"
  ids=$(jq -r --arg re "$GPU_RE" ".items | map(select(.$sel)) | sort_by((.model // \"\") | test(\$re) | not) | .[:2][].id" "$OUT/raw-rows.json")
  [ -z "$ids" ] && { echo '{"sample":null,"reason":"no server in this selection"}' > "$f"; continue; }
  for id in $ids; do
    curl -s --max-time 60 -H "Authorization: Bearer $(cat $SS_TOKEN_FILE)" "$SS_URL/api/v1/servers/$id" | jq "$TRIM" > "$f"
    [ "$(jq '.hardware.gpu_count // 0' "$f")" -gt 0 ] && break
  done
done
```
Writes `P-SS-3-{AVAILABLE,INSTALLED,INSTALLED_TO_INVENTORY,REDFISH_STANDALONE}.json`. Look for:
- Every key is present (`null` values are fine).
- Raw GPU `model` strings, and whether `power_watts` is ever set.
- GPU `health` vocabulary: `UP/DOWN` from Cisco, `HEALTHY/CRITICAL` from Redfish.
- The DGX `vendor`; `standalone` is expected.

**P-SS-4: events, and whether the token can read the audit trail.** Answers open question 5 and gives milestone evidence.
```bash
for t in OPENSHIFT_STATE_CHANGED SERVER_CREATED; do
  code=$(curl -s --max-time 60 -o "$OUT/raw-ev-$t.json" -w '%{http_code}' -H "Authorization: Bearer $(cat $SS_TOKEN_FILE)" "$SS_URL/api/v1/events?event_type=$t&page_size=5")
  echo "$t http=$code"
  [ "$code" = 200 ] && jq '{has_more:.page.has_more, items:[.items[]|{event_type,server_name,actor:(.actor|{type,id}),created_at,data}]}' "$OUT/raw-ev-$t.json" > "$OUT/P-SS-4-$t.json"
done | tee "$OUT/P-SS-4-status.txt"
SINCE=$(date -u -d '7 days ago' +%FT%TZ 2>/dev/null || date -u -v-7d +%FT%TZ)
curl -s --max-time 60 -H "Authorization: Bearer $(cat $SS_TOKEN_FILE)" "$SS_URL/api/v1/events?event_type=OPENSHIFT_STATE_CHANGED&since=$SINCE&page_size=200" \
 | jq --arg s "$SINCE" '{since:$s, first_page:(.items|length), has_more:.page.has_more,
     by_transition:(.items|group_by("\(.data.from)->\(.data.to)")|map({("\(.[0].data.from)->\(.[0].data.to)"): length})|add)}' > "$OUT/P-SS-4-7d.json"
```
Writes `P-SS-4-{status.txt,OPENSHIFT_STATE_CHANGED.json,SERVER_CREATED.json,7d.json}`. Look for:
- HTTP status: `200` means audit access works, `403` means the token is VIEWER rather than AUDITOR, `401` means the token is wrong.
- `data` keys `{from,to,cluster_name}` and `{vendor,name}`, and the 7-day volume of state changes.

**P-SS-5: fleet gauges.** Answers the §20.4 freshness and coverage signals, used by §20.2 rows 2 and 12. If the route returns 404 for `/metrics`, run the same metric names through Thanos or Grafana; do not use `oc exec`.
```bash
curl -s --max-time 60 -H "Authorization: Bearer $(cat $SS_TOKEN_FILE)" "$SS_URL/metrics" \
 | grep -E '^server_scan_(collector_last_seen_timestamp_seconds|collector_last_run_timestamp_seconds|collector_last_run_partial|membership_last_run_(unmatched|partial|timestamp_seconds)|servers_stale|servers\{|openshift_contested_servers|duplicate_name_groups|audit_oldest_event_timestamp_seconds)' \
 | awk -v now="$(date +%s)" '/timestamp_seconds/ {printf "%s  age_h=%.1f\n", $0, (now-$2)/3600; next} {print}' > "$OUT/P-SS-5-metrics.txt"
```
Writes `P-SS-5-metrics.txt`. Look for:
- For every provider, a `last_seen` age under about 3 h and `partial` equal to 0.
- A `membership_*` series for every target cluster, with `unmatched` 0 and an age under about 15 min.
- The oldest audit event: older than 180 days means the purge is report-only.

**P-SS-6: empty and duplicate serials, which affect the asset key `<vendor>:<serial_normalized>`.** Answers §20.10 "serial normalisation". No extra GETs: the normalised serial is recomputed from `rows` the way server-scan does it (lower-case, whitespace collapsed).
```bash
jq '["","0123456789","default string","to be filled by o.e.m.","to be filled by o.e.m","not specified","none","n/a","unknown","system serial number"] as $ph
 | [.items[] | {vendor, src:(.source_provider // "null"), s:((.serial // "") | [splits("\\s+")] | map(select(length>0)) | join(" ") | ascii_downcase)}] as $r
 | {total:($r|length),
    empty_by_src:($r|map(select(.s==""))|group_by(.src)|map({(.[0].src): length})|add),
    placeholder_by_src:($r|map(select(.s!="" and (.s as $s | any($ph[]; .==$s))))|group_by(.src)|map({(.[0].src): length})|add),
    dup_same_vendor_groups:($r|map(select(.s!=""))|group_by([.vendor,.s])|map(select(length>1))|length),
    dup_cross_vendor_groups:($r|map(select(.s!=""))|group_by(.s)|map(select((map(.vendor)|unique|length)>1))|length),
    cross_vendor_pairs:($r|map(select(.s!=""))|group_by(.s)|map(select((map(.vendor)|unique|length)>1)|map("\(.vendor)/\(.src)")|unique|join("+"))|group_by(.)|map({(.[0]): length})|add)}' \
 "$OUT/raw-rows.json" > "$OUT/P-SS-6-serials.json"
```
Writes `P-SS-6-serials.json`. Look for:
- `empty_by_src`: these servers need a fallback asset key.
- Placeholder serials from non-Redfish collectors.
- `dup_same_vendor_groups` should be 0.
- Cross-vendor pairs: the same machine collected under two vendors.

**P-SS-7: auth mode and API version.** Answers open question 5.
```bash
{ echo "no-token /api/v1/servers?page_size=1 -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$SS_URL/api/v1/servers?page_size=1")"
  echo "token    /api/v1/sites             -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -H "Authorization: Bearer $(cat $SS_TOKEN_FILE)" "$SS_URL/api/v1/sites")"
  curl -s --max-time 30 -H "Authorization: Bearer $(cat $SS_TOKEN_FILE)" "$SS_URL/openapi.json" | jq -c '.components.schemas |
    {has_IDRAC_RESET:(.EventType.enum|index("IDRAC_RESET") != null), has_raid_health:(.Health.properties|has("raid")), states:.OpenShiftState.enum}'
} > "$OUT/P-SS-7-auth-version.txt"
```
Writes `P-SS-7-auth-version.txt`. Look for:
- The no-token request: `401` means auth is on; `200` means auth is off and every caller is ADMIN, so report it.
- `has_IDRAC_RESET: true` means the API is ≥ v4.43.0 (`defe172`).

**Redaction before pasting.**
- **Hashed:** hostnames, server names, serials, UUIDs, IPs, MACs, BMC hosts and registry hosts become `h_<10 hex>`: an HMAC with the local salt in `~/.ttv/salt`, the same scheme as Part C, so one server hashes identically in both parts. The salt never leaves the network.
- **Kept:** field names, counts, enums, GPU models, policy keys, template, cluster and MCE names, `srv_…` ids and timestamps.
- **The `perl` pass** also catches IPs, MACs, UUIDs and FQDNs of three or more labels left inside longer strings.
```bash
cd "$OUT" && mkdir -p redacted && export SALT="$(cat ~/.ttv/salt)"
h() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | openssl dgst -sha256 -hmac "$SALT" -r | cut -c1-10; }
KEYRE='^(name|server_name|serial|serial_normalized|system_uuid|bmc_host|macs|mac|host|reported_name|openshift_reported_name|contested_name|display)$'
VALRE='^(([0-9]{1,3}\.){3}[0-9]{1,3}|([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}|[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$'
for f in P-SS-*.json; do
  jq -r --arg k "$KEYRE" --arg v "$VALRE" 'paths(scalars) as $p | getpath($p) as $x | select($x|type=="string")
    | select((([$p[]|strings]|last // "" | test($k)) and (($p|map(tostring)|join("."))|test("profile_template")|not)) or ($x|test($v))) | $x' "$f" \
  | sort -u | while IFS= read -r x; do printf '%s\th_%s\n' "$x" "$(h "$x")"; done \
  | jq -R 'split("\t") | {(.[0]): .[1]}' | jq -s 'add // {}' > .redact-map.json
  jq --slurpfile m .redact-map.json 'walk(if type=="string" and ($m[0][.] != null) then $m[0][.] else . end)' "$f" > "redacted/$f"
done
cp P-SS-*.txt redacted/ 2>/dev/null; rm -f .redact-map.json
for f in redacted/P-SS-*; do perl -MDigest::SHA=hmac_sha256_hex -i -pe \
  's/\b((?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[a-z0-9-]+(?:\.[a-z0-9-]+)+\.[a-z]{2,})\b/"h_".substr(hmac_sha256_hex(lc($1),$ENV{SALT}),0,10)/gie' "$f"; done
grep -rEn '([0-9]{1,3}\.){3}[0-9]{1,3}' redacted/ || echo "no IPv4 left"; ls redacted/
```
Before pasting, read each file once for free text (`maintenance.reason`, `site_id`), then paste only `redacted/*`.

## Part B: managers (UCS Central, OpenManage Enterprise)

**Tested 2026-10-30** without a real appliance: the B5 parser against server-scan's own dry-run printer (`defe172`) over its fake provider, matching the true counts for both vendors and for a server with no profile; the generated Job accepted by an OpenShift 4.20 API server (server-side dry-run, nothing saved); the B3/B4 logic on small fixtures (which caught one bug, now fixed). Not yet run against a real UCS Central, OpenManage or iDRAC.

**Question answered:** how many GPU servers sit in UCS Central and OpenManage with **no profile**, a **non-OCP profile**, or an **OCP profile** (the simplified collector rule, `docs/discovery.md` Inputs 2026-10-30).

**Built from server-scan's own code** (`defe172`), because server-scan already reads these managers in production:
- **Cisco.** The UCS Central collector logs in to every registered UCS Manager domain and reads each server, its bound service profile and its GPUs (`graphicsCard`). A server without a profile is named after its hardware object or DN, so only the `^ocp` name filter drops it (`backend/app/infrastructure/providers/ucs_manager/mapping.py:60-75,474`). Clearing the filter lists every server.
- **Dell.** The OpenManage collector finds servers through their OME **profiles** (the printed name is the `ProfileName`, `openmanage/mapping.py:184`) and reads hardware, GPUs included, from each server's iDRAC over Redfish (ADR-0020). Clearing the filter lists every Dell server that has a profile; servers with **no** profile need B3 and B4.
- **GPU rule** (B4 copies it from `redfish/mapping.py:556-870`): a Redfish `Processor` with `ProcessorType == "GPU"` that is not `Absent`; if none, a `PCIeDevice` whose `Manufacturer` starts with `10DE`, `1002` or `8086` and whose `Description` contains VGA, GPU, 3D or DISPLAY.

**The one step that creates something.** B1 and B2 run server-scan's collector as a temporary Job copied from its own CronJob, adding `--dry-run` ("print what each manager reports and exit without writing anything", `tools/run_collector.py`) and clearing the name pattern. The Job reads the managers with server-scan's accounts, network policy and secrets, writes nothing, and is deleted at the end of the block. Everything else in Part B is GET-only (plus logging in and out of OpenManage).

**Setup** (edit the first four lines; needs `oc` logged in to the cluster that runs server-scan, plus `curl`, `jq`, `python3`)
```bash
export NS=server-scan REL=server-scan NAME_PATTERN='^ocp'   # server-scan namespace and Helm release; pattern from P-SS-1
export OME_HOST=ome.example.internal                          # OpenManage Enterprise host
export OME_CRED=~/.ttv/ome.cred IDRAC_CRED=~/.ttv/idrac.curlrc  # server-scan's accounts; see below
export CA_BUNDLE=                                             # PEM for OME/iDRAC certificates; empty = -k (note it in the paste)
export OUT=$HOME/ttv-discovery/managers-$(date -u +%Y%m%d); mkdir -p "$OUT/raw"; umask 077
[ -n "$CA_BUNDLE" ] && TLS=(--cacert "$CA_BUNDLE") || TLS=(-k)
# ~/.ttv/ome.cred: line 1 user, line 2 password.  ~/.ttv/idrac.curlrc: one line, user = "name:password"
dryrun() {  # $1 = CronJob suffix, $2 = Job name. Copies server-scan's collector CronJob, adds --dry-run, clears the name filter.
  oc -n "$NS" get cronjob "$REL-$1" -o json | jq --arg n "$2" '{apiVersion:"batch/v1", kind:"Job",
    metadata:{name:$n, labels:{"app.kubernetes.io/part-of":"ttv-discovery"}},
    spec:{backoffLimit:0, ttlSecondsAfterFinished:86400, template:(.spec.jobTemplate.spec.template | .spec.restartPolicy="Never"
      | .spec.containers[0].command += ["--dry-run"]
      | .spec.containers[0].env = ([.spec.containers[0].env[]? | select(.name|test("NAME_PATTERN$")|not)]
          + [{name:"INVENTORY_COLLECTOR_NAME_PATTERN", value:""}]))}}' > "$OUT/raw/$2.json"
  oc -n "$NS" create -f "$OUT/raw/$2.json" || return 1
  until s=$(oc -n "$NS" get job "$2" -o jsonpath='{.status.succeeded}{.status.failed}'); [ -n "$s" ]; do sleep 20; done
  oc -n "$NS" logs "job/$2" > "$OUT/raw/$2.log"; echo "$2: $(grep -c '^\[[0-9]' "$OUT/raw/$2.log") servers printed"
  oc -n "$NS" delete job "$2"
}
```

**B1: Cisco, every server in UCS Central's domains** → `raw/ttv-dryrun-ucsc.log`
```bash
dryrun collector-ucs-central ttv-dryrun-ucsc
```

**B2: Dell, every server that has an OpenManage profile** → `raw/ttv-dryrun-ome.log`. Takes as long as server-scan's own OpenManage run, since it reads each iDRAC.
```bash
dryrun collector-openmanage ttv-dryrun-ome
```

**B3: Dell servers with no profile** (OpenManage devices that no profile targets) → `raw/ome-noprofile.tsv`, `B3-ome.json`
```bash
{ IFS= read -r U; IFS= read -r P; } < "$OME_CRED"
printf '{"UserName":"%s","Password":"%s","SessionType":"API"}' "$U" "$P" | curl -sS -f "${TLS[@]}" -D "$OUT/raw/.h" -o "$OUT/raw/.s" \
  -H 'Content-Type: application/json' --data-binary @- "https://$OME_HOST/api/SessionService/Sessions"; unset P
printf 'X-Auth-Token: %s\n' "$(awk 'tolower($1)=="x-auth-token:"{print $2}' "$OUT/raw/.h" | tr -d '\r')" > "$OUT/raw/.hdr"; SID=$(jq -r .Id "$OUT/raw/.s"); rm -f "$OUT/raw/.h" "$OUT/raw/.s"
ome_all() { local next=$1; : > "$2"; while [ -n "$next" ]; do curl -sS -f "${TLS[@]}" -H @"$OUT/raw/.hdr" "https://$OME_HOST$next" > "$2.p" || { echo "FAILED $next"; break; }
  jq -c '.value[]' "$2.p" >> "$2"; next=$(jq -r '."@odata.nextLink" // empty' "$2.p"); next=${next#https://$OME_HOST}; done; rm -f "$2.p"; echo "$(wc -l < "$2") <- $1"; }
ome_all "/api/DeviceService/Devices?\$filter=Type%20eq%201000&\$top=500" "$OUT/raw/ome-servers.jsonl"
ome_all "/api/ProfileService/Profiles?\$top=500" "$OUT/raw/ome-profiles.jsonl" || ome_all "/api/TemplateService/Profiles?\$top=500" "$OUT/raw/ome-profiles.jsonl"  # OME 3.x path
curl -sS "${TLS[@]}" -o /dev/null -w 'OME logout HTTP %{http_code}\n' -X DELETE -H @"$OUT/raw/.hdr" "https://$OME_HOST/api/SessionService/Sessions('$SID')"; rm -f "$OUT/raw/.hdr"
jq -rs --slurpfile p "$OUT/raw/ome-profiles.jsonl" '($p|map(.TargetId|select(.!=null and .>0)|tostring)) as $ids | ($p|map(.TargetName//empty)) as $names
  | .[] | (.DeviceManagement[0].NetworkAddress // "") as $ip | (.DeviceName // "") as $dn | (.Id|tostring) as $i
  | select(($ids|index($i)) == null and ($names|index($ip)) == null and ($names|index($dn)) == null)
  | [.Id, (.Model // "-"), $ip] | @tsv' "$OUT/raw/ome-servers.jsonl" > "$OUT/raw/ome-noprofile.tsv"
jq -n --slurpfile s <(jq -s . "$OUT/raw/ome-servers.jsonl") --slurpfile p <(jq -s . "$OUT/raw/ome-profiles.jsonl") --rawfile np "$OUT/raw/ome-noprofile.tsv" \
  '{servers:($s[0]|length), profiles:($p[0]|length), types:($s[0]|map(.Type)|unique), no_profile:($np|split("\n")|map(select(length>0))|length),
    no_profile_by_model:($np|split("\n")|map(select(length>0)|split("\t")[1])|group_by(.)|map({(.[0]):length})|add)}' > "$OUT/B3-ome.json"; cat "$OUT/B3-ome.json"
```
`types` must be `[1000]` (servers only); anything else means the filter was ignored.

**B4: GPUs on the no-profile Dell servers**, read from each iDRAC with server-scan's rule (GET only) → `B4-idrac-gpus.json`
```bash
rf() { curl -sS -f --max-time 30 "${TLS[@]}" -K "$IDRAC_CRED" -H 'Accept: application/json' "$1"; }
idrac_gpus() { local h=$1 sys n
  sys=$(rf "https://$h/redfish/v1/Systems" | jq -r '.Members[0]."@odata.id" // empty') || return; [ -n "$sys" ] || return
  n=$(rf "https://$h$sys/Processors" | jq -r '.Members[]."@odata.id"' | while read -r p; do rf "https://$h$p"; done \
      | jq -s '[.[] | select(.ProcessorType=="GPU" and .Status.State!="Absent")] | length')
  [ "${n:-0}" -gt 0 ] || n=$(rf "https://$h$sys" | jq -r '.PCIeDevices[]?."@odata.id"' | while read -r p; do rf "https://$h$p"; done \
      | jq -s '[.[] | select(.Status.State!="Absent" and (((.Manufacturer//"")|ascii_upcase|.[0:4]) as $v | ["10DE","1002","8086"]|index($v)) and ((.Description//"")|ascii_upcase|test("VGA|GPU|3D|DISPLAY")))] | length')
  echo "$n"; }
while IFS=$'\t' read -r id model ip; do printf '%s\t%s\n' "$model" "$( [ -n "$ip" ] && idrac_gpus "$ip" || echo)"; done < "$OUT/raw/ome-noprofile.tsv" > "$OUT/raw/idrac-gpus.tsv"
jq -Rn '[inputs|split("\t")|{m:.[0], g:.[1]}] | group_by(.m) | map({(.[0].m): {checked:length, with_gpu:map(select((.g|tonumber? // 0)>0))|length,
  unread:map(select(.g==""))|length}}) | add' "$OUT/raw/idrac-gpus.tsv" > "$OUT/B4-idrac-gpus.json"; cat "$OUT/B4-idrac-gpus.json"
```

**B5: counts from the dry-runs** → `B5-dryrun.json` (counts and GPU models only; no names, serials or addresses)
```bash
cat > ~/.ttv/dryrun_summary.py <<'PY'
import collections, json, re, sys
pat, out = re.compile(sys.argv[1]), {}
for path in sys.argv[2:]:
    dell = "ome" in path
    buckets, models, managers, errors, cur = collections.Counter(), collections.Counter(), 0, 0, None
    def close(c):
        if c is None: return
        b = "NO_PROFILE" if not (c["profile"] or dell) else ("OCP_PROFILE" if pat.search(c["name"]) else "NON_OCP_PROFILE")
        buckets[b + "/" + ("gpu_unread" if c["gpus"] is None else "gpu" if c["gpus"] else "no_gpu")] += 1
        for m in c["models"]: models[b + "/" + m] += 1
    for line in open(path, errors="replace"):
        line = line.rstrip("\n")
        if line.startswith("=== "): managers += 1; continue
        if (m := re.match(r"^\[\d+\] (.*)$", line)): close(cur); cur = {"name": m[1], "profile": False, "gpus": None, "models": []}; continue
        if re.search(r"(?i)traceback|\berror\b", line) and not line.startswith("     "): errors += 1
        if cur is None: continue
        if re.match(r"^\s+profile\s+:", line): cur["profile"] = True
        elif (m := re.match(r"^\s+gpus\s+:\s+(\d+)", line)): cur["gpus"] = int(m[1])
        elif (m := re.match(r"^\s+gpu (.+?)  vendor=", line)): cur["models"].append(m[1])
    close(cur)
    out["OPENMANAGE" if dell else "UCS_CENTRAL"] = {"managers": managers, "servers": sum(buckets.values()),
        "buckets": dict(sorted(buckets.items())), "gpu_models": dict(sorted(models.items())), "error_lines": errors}
print(json.dumps(out, indent=1))
PY
python3 -I ~/.ttv/dryrun_summary.py "$NAME_PATTERN" "$OUT"/raw/ttv-dryrun-*.log > "$OUT/B5-dryrun.json"; cat "$OUT/B5-dryrun.json"
```
**Bring back:** `B3-ome.json`, `B4-idrac-gpus.json`, `B5-dryrun.json`. They hold counts and models only, so they need no redaction; read them once anyway. `raw/` stays inside the network.

**What to look for**
- `UCS_CENTRAL` `NO_PROFILE/gpu`: Cisco GPU servers for DC's queue. `NON_OCP_PROFILE/gpu`: "other intended use". `OCP_PROFILE/gpu` should be close to server-scan's Cisco GPU count (P-SS-2).
- `OPENMANAGE` `NON_OCP_PROFILE/gpu` and `OCP_PROFILE/gpu` from B5, and `with_gpu` per model from B4 (no-profile Dell GPU servers for DC's queue).
- `gpu_unread` or `unread` above 0: the collector could not read GPUs for those servers. `error_lines` above 0: read the end of the raw log.

## Part C: target GPU clusters

**Lab-tested 2026-10-30** end to end against the sandbox cluster (OpenShift 4.20.35; no GPUs, Run:ai or KServe, so GPU-specific queries returned empty results). Two fixes came from that run: R8 handles clusters without the GPU Operator, and R12 also hashes Machine, BareMetalHost and Agent names.

Run once per target cluster from a bastion inside the air-gapped network (bash, `oc` ≥ 4.16, `jq`, `curl` ≥ 7.55, `python3`). Every command is `get`/`api-resources`/`explain`/`whoami`, or an HTTP GET to Thanos. **Not used:** `oc exec`, `oc create` (including `oc create token`), `apply`, `patch`, `label`, `debug`, `rsh`, `port-forward`, `oc adm`. Only stdout and local files are written.

**Token** (existing tokens only): either (a) your own session token, if your user holds `cluster-monitoring-view`: `(umask 077; oc whoami -t > "$TOKEN_FILE")`; or (b) the ServiceAccount token Secret from the planned `deploy/gpu-cluster-rbac/` step, which you apply yourself first: `oc -n <ns> get secret <name> -o jsonpath='{.data.token}' | base64 -d > "$TOKEN_FILE"`.

**R0 — variables and helpers** (edit the first block; output tree `$OUT/{meta,api,nodes,pods,owners,runai,serving,mon,prom,i2}`)
```bash
export CLUSTER=ocp4-prod-gpu-site1            # must equal server-scan openshift.cluster_name
export KUBECONFIG=$HOME/.kube/config-$CLUSTER  # existing login: cluster-reader (or equivalent get/list) + cluster-monitoring-view
export TOKEN_FILE=$HOME/.ttv/$CLUSTER.token    # existing bearer token, mode 600 (see above)
export BASE_DOMAINS=corp.example               # comma list of DNS suffixes to redact
export SERVERSCAN_EXPORT=                      # optional: JSON from server-scan GET /api/v1/servers/rows (R11)
export PACE=30                                 # seconds between Thanos calls (Red Hat: ≤ 1 query / 30 s)
# ---- derived; do not edit ----
mkdir -p ~/.ttv && chmod 700 ~/.ttv; [ -s "$TOKEN_FILE" ] || echo "!! $TOKEN_FILE missing: create it (see Token above), then rerun R0"; [ -f ~/.ttv/salt ] || (umask 077; head -c 32 /dev/urandom | base64 > ~/.ttv/salt)
export OUT=$HOME/ttv-discovery/$CLUSTER-$(date -u +%Y%m%dT%H%MZ)
mkdir -p "$OUT"/{meta,api,nodes,pods,owners,runai,serving,mon,prom,i2}
export THANOS_URL=https://$(oc -n openshift-monitoring get route thanos-querier -o jsonpath='{.status.ingress[0].host}')
oc -n openshift-config-managed get cm default-ingress-cert -o jsonpath='{.data.ca-bundle\.crt}' > ~/.ttv/$CLUSTER-ca.crt
export CA=~/.ttv/$CLUSTER-ca.crt HDR=~/.ttv/$CLUSTER.hdr
(umask 077; printf 'Authorization: Bearer %s\n' "$(cat "$TOKEN_FILE")" > "$HDR")
set -o pipefail   # so a failed oc in a pipe is reported, not hidden by jq
og() { local f=$1; shift; oc get "$@" -o json > "$OUT/$f" 2> "$OUT/$f.err" && echo "ok  $f items=$(jq '.items|length' "$OUT/$f")" || echo "ERR $f: $(head -c 160 "$OUT/$f.err")"; }
_p() { jq -r '"\(.status) series=\(.data.result|length) \(.error // "")"' "$1"; sleep "$PACE"; }
q()  { curl -sS -G --cacert "$CA" -H @"$HDR" "$THANOS_URL/api/v1/query" --data-urlencode "query=$2" > "$OUT/prom/$1.json"; echo "$1: $(_p "$OUT/prom/$1.json")"; }
qr() { local e; e=$(date +%s); curl -sS -G --cacert "$CA" -H @"$HDR" "$THANOS_URL/api/v1/query_range" --data-urlencode "query=$2" \
       --data-urlencode "start=$((e-$3))" --data-urlencode "end=$e" --data-urlencode "step=$4" > "$OUT/i2/$1.json"; echo "$1: $(_p "$OUT/i2/$1.json")"; }
q 00-auth 'vector(1)'      # expect "success series=1"; 401/403 = token or RBAC problem; stop and report
```
If the CA extraction fails, put the ingress CA bundle you already trust in `$CA`. Do not fall back to `-k` silently; note it in the paste.

**R1 — cluster identity and versions** → `meta/`
```bash
oc version -o json > "$OUT/meta/version.json"
oc get clusterversion version -o json | jq '{clusterID:.spec.clusterID, version:.status.desired.version, history:[.status.history[]|{version,state,completionTime}]}' > "$OUT/meta/clusterversion.json"
oc get infrastructure cluster -o json | jq '{platform:.status.platformStatus.type, cp:.status.controlPlaneTopology, infra:.status.infrastructureTopology}' > "$OUT/meta/infra.json"
oc get csv -A -o json | jq '[.items[]|select(.metadata.name|test("gpu|nvidia|nfd|run|kai|rhods|opendatahub|kserve|serverless|servicemesh|leaderworker|llm|lws"))|{name:.metadata.name, ns:.metadata.namespace, version:.spec.version, phase:.status.phase}]|unique_by(.name)' > "$OUT/meta/csv.json"
```
**R2 — API discovery** → `api/`. An empty Run:ai list is a valid result (Run:ai not installed on this cluster), not an error.
```bash
oc api-resources -o wide > "$OUT/api/api-resources.txt" 2> "$OUT/api/api-resources.err"; oc api-versions > "$OUT/api/api-versions.txt"
for g in run.ai scheduling.run.ai; do oc api-resources --api-group=$g -o wide > "$OUT/api/group-$g.txt"; echo "$g: $(grep -cv -e '^NAME' -e '^$' "$OUT/api/group-$g.txt") resources"; done
oc get crd -o json | jq -r '.items[]|select(.spec.group|test("run\\.ai|kserve|inference|llm-d|nvidia|metal3|agent-install|hypershift|kubeflow|leaderworkerset|knative|ray|gateway"))|[.spec.group,.spec.names.kind,.spec.names.plural,.spec.scope,([.spec.versions[]|select(.served)|.name+(if .storage then "*" else "" end)]|join(","))]|@tsv' | sort > "$OUT/api/crds.tsv"
for r in $(oc api-resources --api-group=run.ai -o name); do oc explain "$r" --recursive > "$OUT/api/explain-$r.txt" 2>&1; done
```
**R3 — nodes** → `nodes/` (raw `nodes.json` stays local; paste the `.tsv` files after R12)
```bash
og nodes/nodes.json nodes
jq -r '.items[]|[.metadata.name, (.metadata.labels["kubernetes.io/hostname"]//"-"), (.status.nodeInfo.systemUUID//"-"),
  ([.status.conditions[]|select(.type=="Ready")|.status+"@"+.lastTransitionTime][0]), (.spec.unschedulable//false),
  (.status.capacity["nvidia.com/gpu"]//"-"), (.status.allocatable["nvidia.com/gpu"]//"-"), (.metadata.labels["nvidia.com/gpu.product"]//"-"),
  (.metadata.labels["nvidia.com/mig.strategy"]//"-"), (.metadata.labels["nvidia.com/gpu.sharing-strategy"]//"-"),
  (.metadata.annotations["machine.openshift.io/machine"]//"-"), ([.spec.taints[]?|.key+"="+(.value//"")+":"+.effect]|join(";"))]|@tsv' \
  "$OUT/nodes/nodes.json" > "$OUT/nodes/nodes.tsv"
jq -r '[.items[]|select(.metadata.labels["nvidia.com/gpu.present"]=="true" or .status.capacity["nvidia.com/gpu"]!=null)|.metadata.labels|to_entries[]|select(.key!="kubernetes.io/hostname")|.key+"="+.value]|group_by(.)|map("\(length)\t\(.[0])")[]' "$OUT/nodes/nodes.json" > "$OUT/nodes/gpu-node-labels.tsv"
jq -r '.items[]|.status.allocatable|keys[]|select(test("nvidia|gpu"))' "$OUT/nodes/nodes.json" | sort | uniq -c > "$OUT/nodes/gpu-resource-names.txt"
for r in machines.machine.openshift.io baremetalhosts.metal3.io agents.agent-install.openshift.io; do
  oc get "$r" -A -o json 2>/dev/null | jq '[.items[]|{kind, ns:.metadata.namespace, name:.metadata.name, consumer:.spec.consumerRef.name, nodeRef:.status.nodeRef.name,
    ann:(.metadata.annotations//{}|with_entries(select(.key|test("metal3|machine|agent")))), state:(.status.provisioning.state//.status.phase),
    vendor:(.status.hardware.systemVendor//.status.inventory.systemVendor), host:(.spec.hostname//.status.inventory.hostname), cd:.spec.clusterDeploymentName.name}]' > "$OUT/nodes/$r.json" \
  && echo "$r: $(jq length "$OUT/nodes/$r.json")" || echo "$r: not present or not readable"
done
```
**R4 — GPU-relevant pods, minimal fields only** → `pods/` (env, args and secrets are never saved)
```bash
oc get pods -A -o json | jq '[.items[]|{ns:.metadata.namespace, name:.metadata.name, uid:.metadata.uid, node:.spec.nodeName, phase:.status.phase,
  sched:.spec.schedulerName, prio:.spec.priorityClassName, start:.status.startTime, ready:([.status.conditions[]?|select(.type=="Ready")|.status][0]),
  restarts:([.status.containerStatuses[]?.restartCount]|add), claims:.spec.resourceClaims,
  gpu:[(.spec.containers+(.spec.initContainers//[]))[]|.name as $c|(.resources.limits//{})|to_entries[]|select(.key|test("nvidia|gpu"))|"\($c):\(.key)=\(.value)"],
  owners:[.metadata.ownerReferences[]?|{apiVersion,kind,name,uid,controller}], labels:(.metadata.labels//{}),
  ann:(.metadata.annotations//{}|with_entries(select(.key|test("run\\.ai|runai|gpu|kserve|serving|llm-d|inference|nvidia")))),
  images:[.spec.containers[].image], vllm:([.spec.containers[]|(.image+" "+((.command//[])+(.args//[])|join(" ")))|test("vllm")]|any)}]' > "$OUT/pods/pods-min.json"
jq '[.[]|select((.gpu|length>0) or (.claims!=null) or (.ann|length>0) or (.ns|test("runai")) or .vllm)]' "$OUT/pods/pods-min.json" > "$OUT/pods/gpu-pods.json"
jq -r '.[]|[.ns, .phase, (.gpu|join(",")), (.owners[0].kind//"-"), (.ann|keys|join(","))]|@tsv' "$OUT/pods/gpu-pods.json" | sort | uniq -c | sort -rn > "$OUT/pods/gpu-pod-shapes.tsv"
jq -r '[.[]|.labels|keys[]]|group_by(.)|map("\(length)\t\(.[0])")[]' "$OUT/pods/gpu-pods.json" > "$OUT/pods/gpu-pod-label-keys.tsv"
jq -r '[.[]|.ann|to_entries[]|.key+"="+(.value|tostring|.[0:60])]|group_by(.)|map("\(length)\t\(.[0])")[]' "$OUT/pods/gpu-pods.json" > "$OUT/pods/gpu-pod-annotations.tsv"
```
**R5 — ownership chains** (lists each owner kind once, cluster-wide, up to 5 levels) → `owners/`
```bash
cd "$OUT/owners" && : > objs.jsonl && : > fetched.txt
jq -r '.[].owners[]|.apiVersion+" "+.kind' ../pods/gpu-pods.json | sort -u > want.txt
for depth in 1 2 3 4 5; do
  comm -23 want.txt fetched.txt > todo.txt; [ -s todo.txt ] || break
  while read -r av kind; do
    k=$(echo "$kind" | tr '[:upper:]' '[:lower:]'); case "$av" in */*) r="$k.${av%/*}";; *) r="$k";; esac
    oc get "$r" -A -o json 2>/dev/null | jq -c '.items[]|{apiVersion,kind,ns:.metadata.namespace,name:.metadata.name,uid:.metadata.uid,
      owners:[.metadata.ownerReferences[]?|{apiVersion,kind,name,uid}], labels:(.metadata.labels//{})}' >> objs.jsonl || echo "cannot list $r"
    echo "$av $kind" >> fetched.txt
  done < todo.txt
  sort -u -o fetched.txt fetched.txt
  { cat want.txt; jq -r '.owners[]|.apiVersion+" "+.kind' objs.jsonl; } | sort -u > want.new && mv want.new want.txt
done
jq -s --slurpfile pods ../pods/gpu-pods.json 'def up($o;$u;$d): if $d>6 or $o[$u]==null then [] else [$o[$u]|.kind+"/"+.name]+(($o[$u].owners[0].uid//null) as $n|if $n then up($o;$n;$d+1) else [] end) end;
  (map({key:.uid,value:.})|from_entries) as $o | [$pods[0][]|{ns, pod:.name, node, gpu, prio, chain:(if (.owners|length)>0 then [.owners[0].kind+"/"+.owners[0].name]+(up($o;.owners[0].uid;0)[1:]) else [] end)}]' objs.jsonl > chains.json
jq -r '.[]|[.ns, (.prio//"-"), (["Pod"]+(.chain|map(split("/")[0]))|join(">"))]|@tsv' chains.json | sort | uniq -c | sort -rn > chain-shapes.tsv; cd - >/dev/null
```
**R6 — Run:ai** → `runai/`. On a cluster without Run:ai this writes `NO_RUNAI` and still records leftovers (a `scheduling.run.ai` group alone can be standalone KAI-Scheduler, not Run:ai).
```bash
if grep -qv -e '^NAME' -e '^$' "$OUT/api/group-run.ai.txt"; then
  for r in $(oc api-resources --api-group=run.ai -o name) $(oc api-resources --api-group=scheduling.run.ai -o name); do
    oc get "$r" -A -o json | jq -c --arg r "$r" '{r:$r, n:(.items|length), labelKeys:([.items[].metadata.labels//{}|keys[]]|unique),
      ownerKinds:([.items[].metadata.ownerReferences[]?.kind]|unique), status:([.items[]|(.status.phase//.status.state//"?")|tostring]|group_by(.)|map({(.[0]):length})|add)}' \
      || echo "{\"r\":\"$r\",\"error\":true}"
  done > "$OUT/runai/kinds-summary.jsonl"
  oc get runaiconfigs.run.ai -A -o json | jq '[.items[]|{ns:.metadata.namespace, name:.metadata.name, labels:.metadata.labels}]' > "$OUT/runai/runaiconfig.json"
else echo NO_RUNAI > "$OUT/runai/status.txt"; fi
oc get ns -o json | jq -r '.items[]|select((.metadata.name|test("^runai|^kai")) or (.metadata.labels//{}|keys|any(test("run\\.ai|runai|kai\\.scheduler"))))|[.metadata.name, (.metadata.labels//{}|with_entries(select(.key|test("run\\.ai|runai|kai")))|tostring)]|@tsv' > "$OUT/runai/namespaces.tsv"
jq '[.[]|select(.ns|test("runai-reservation|kai-resource-reservation"))|{node, phase, gpu, labels, ann, owner:(.owners[0].kind//null)}]' "$OUT/pods/gpu-pods.json" > "$OUT/runai/reservation-pods.json"
jq -r '.[]|select((.labels|keys|any(startswith("runai-gpu-group"))) or (.ann|has("gpu-fraction")))|[.ns, .phase, (.labels|to_entries|map(select(.key|startswith("runai-gpu-group")))|from_entries|tostring), (.ann["gpu-fraction"]//"-"), (.ann["gpu-memory"]//"-"), (.owners[0].kind//"-")]|@tsv' "$OUT/pods/gpu-pods.json" > "$OUT/runai/fractional-pods.tsv"
```
**R7 — serving stacks** → `serving/` (absent resources are recorded, not errors)
```bash
for r in inferenceservices.serving.kserve.io llminferenceservices.serving.kserve.io servingruntimes.serving.kserve.io inferencepools.inference.networking.k8s.io \
         inferencepools.inference.networking.x-k8s.io inferenceobjectives.inference.networking.x-k8s.io inferencemodels.inference.networking.x-k8s.io httproutes.gateway.networking.k8s.io; do
  oc get "$r" -A -o json 2>/dev/null | jq -c --arg r "$r" '{r:$r, items:[.items[]|{ns:.metadata.namespace, name:.metadata.name, apiVersion,
    mode:(.metadata.annotations["serving.kserve.io/deploymentMode"]//null), ready:([.status.conditions[]?|select(.type=="Ready")|.status][0])}]}' || echo "{\"r\":\"$r\",\"absent\":true}"
done > "$OUT/serving/objects.jsonl"
jq -r '.[]|select(.vllm or (.labels|has("serving.kserve.io/inferenceservice")) or (.labels["app.kubernetes.io/part-of"]=="llminferenceservice"))|[.ns,
  (.labels["serving.kserve.io/inferenceservice"]//.labels["app.kubernetes.io/name"]//"-"), (.labels["llm-d.ai/role"]//"-"), (.labels["app.kubernetes.io/component"]//"-"),
  .phase, .ready, (.gpu|join(",")), (.owners[0].kind//"-"), (.sched//"-"), (.images[0]|split("/")|last)]|@tsv' "$OUT/pods/pods-min.json" > "$OUT/serving/serving-pods.tsv"
```
**R8 — GPU Operator and monitoring configuration** → `mon/`
```bash
GPUNS=$(oc get ds -A -o json | jq -r '[.items[]|select(.metadata.name|test("dcgm-exporter"))|.metadata.namespace][0] // empty'); echo "GPU operator ns: ${GPUNS:-none}"
if [ -n "$GPUNS" ]; then
oc get ns "$GPUNS" -o json | jq '.metadata.labels' > "$OUT/mon/gpu-ns-labels.json"
oc get clusterpolicies.nvidia.com -o json | jq '[.items[]|{state:.status.state, dcgmExporter:.spec.dcgmExporter, mig:.spec.mig, devicePluginConfig:.spec.devicePlugin.config, driver:.spec.driver.version}]' > "$OUT/mon/clusterpolicy.json"
oc -n "$GPUNS" get ds -o json | jq '[.items[]|select(.metadata.name|test("dcgm"))|{name:.metadata.name, hostNetwork:.spec.template.spec.hostNetwork,
  c:[.spec.template.spec.containers[]|{name, image:(.image|split("/")|last), env:[.env[]?|select(.name|test("DCGM|KUBERNETES|NODE_NAME|POD"))|{name,value}]}]}]' > "$OUT/mon/dcgm-ds.json"
CM=$(jq -r '.[0].dcgmExporter.config.name // empty' "$OUT/mon/clusterpolicy.json"); [ -n "$CM" ] && oc -n "$GPUNS" get cm "$CM" -o json | jq -r '.data[]' | grep -v '^#' > "$OUT/mon/dcgm-custom-fields.csv"
else echo NO_GPU_OPERATOR > "$OUT/mon/status.txt"; fi
oc get servicemonitors.monitoring.coreos.com,podmonitors.monitoring.coreos.com -A -o json | jq '[.items[]|select(.metadata.namespace|test("^(openshift-|kube-)")|not)|{kind, ns:.metadata.namespace,
  name:.metadata.name, sel:.spec.selector, eps:[(.spec.endpoints//.spec.podMetricsEndpoints)[]?|{port,path,interval,honorLabels,relabelings}]}]' > "$OUT/mon/monitors.json"
for cm in openshift-monitoring/cluster-monitoring-config openshift-user-workload-monitoring/user-workload-monitoring-config; do
  oc -n "${cm%/*}" get cm "${cm#*/}" -o jsonpath='{.data.config\.yaml}' > "$OUT/mon/${cm#*/}.yaml" 2>/dev/null || echo "$cm not readable (R9 measures retention anyway)"; done
```
**R9 — Thanos queries** → `prom/` (each call waits `$PACE` s)
```bash
q 01-retention-days 'max by (job, namespace) ((time() - prometheus_tsdb_lowest_timestamp_seconds) / 86400)'   # need >= 7 for prometheus-user-workload
curl -sS -G --cacert "$CA" -H @"$HDR" "$THANOS_URL/api/v1/label/__name__/values" | jq -r '.data[]' \
  | grep -Ei '^(DCGM_|vllm:|llm_d_|inference_|kserve|runai_|kn_|revision_|kube_pod_container_resource_requests)' > "$OUT/prom/metric-names.txt"; sleep "$PACE"
q 10-dcgm-sample   'topk(2, DCGM_FI_DEV_POWER_USAGE)'                                                 # full label set of two series
q 11-R-series      'count by (Hostname) (DCGM_FI_DEV_POWER_USAGE)'; q 12-R-uuids       'count by (Hostname) (count by (Hostname, UUID) (DCGM_FI_DEV_POWER_USAGE))'       # differs from 11 => MIG/vGPU duplicates
q 13-no-hostname   'count(DCGM_FI_DEV_POWER_USAGE{Hostname=""}) or vector(0)'
q 14-models        'count by (modelName) (count by (modelName, UUID) (DCGM_FI_DEV_POWER_USAGE))'; q 15-prof          'count by (modelName) (count by (modelName, UUID) (DCGM_FI_PROF_GR_ENGINE_ACTIVE))' # compare with 14
q 16-mig           'count by (modelName, GPU_I_PROFILE) (DCGM_FI_DEV_FB_USED{GPU_I_PROFILE!=""})'
q 17-attr-exported 'count by (exported_namespace) (DCGM_FI_DEV_GPU_UTIL{exported_pod!=""})'; q 18-attr-plain    'count by (namespace) (DCGM_FI_DEV_GPU_UTIL{pod!~"|.*dcgm-exporter.*"})'
q 19-health-fields 'count by (__name__) ({__name__=~"DCGM_FI_DEV_(XID_ERRORS|ECC_.*|.*REMAPPED_ROWS|ROW_REMAP_FAILURE)"})'; q 20-xid-nonzero   'count by (Hostname) (DCGM_FI_DEV_XID_ERRORS > 0)'
q 21-ksm-gpu       'sum by (namespace) (kube_pod_container_resource_requests{resource="nvidia_com_gpu"})'
q 30-vllm-targets  'count by (namespace, job, service, model_name, engine) (vllm:num_requests_running)'
q 31-vllm-24h      'sum by (namespace, service, model_name, finished_reason) (increase(vllm:request_success_total[24h]))'
q 32-vllm-pods-24h 'sum by (namespace, pod) (increase(vllm:request_success_total[24h]))'           # join with serving-pods.tsv roles (P/D double count)
EPP=$(grep -m1 -E '^(llm_d_epp_request_total|inference_objective_request_total|inference_model_request_total)$' "$OUT/prom/metric-names.txt"); echo "EPP counter: ${EPP:-none}"
[ -n "$EPP" ] && q 33-epp-24h "sum by (namespace, job, model_name, target_model_name, fairness_id) (increase($EPP[24h]))"
q 40-runai         'count by (__name__) ({__name__=~"runai_.*"})'
```
**R10 — I2 calibration** (hourly genuine-request distributions per deployment) → `prom/i2-*`, `i2/`. If R9 `01-retention-days` shows < 7 for `prometheus-user-workload`, set `RANGE=1d SPAN=86400` and run R10 once a day for 7 days (new `$OUT` each day).
```bash
RANGE=7d SPAN=604800
V='sum by (namespace, service, model_name) (increase(vllm:request_success_total[1h]))'
G='(sum by (namespace, pod) (kube_pod_container_resource_requests{resource="nvidia_com_gpu"}) > 0)'
P="sum by (namespace, pod) (increase(vllm:request_success_total[1h])) / on (namespace, pod) $G"     # requests per GPU-hour, per pod
qr i2-vllm-hourly "$V" $SPAN 3600
for s in 0.1 0.5 0.9; do q i2-vllm-q$s "quantile_over_time($s, ($V)[$RANGE:1h])"; q i2-pergpu-q$s "quantile_over_time($s, ($P)[$RANGE:1h])"; done
q i2-vllm-hours      "count_over_time(($V)[$RANGE:1h])"
q i2-vllm-zero-hours "count_over_time((($V) == 0)[$RANGE:1h])"
q i2-pergpu-max      "max_over_time(($P)[$RANGE:1h])"
if [ -n "$EPP" ]; then E="sum by (namespace, model_name) (increase($EPP[1h]))"; qr i2-epp-hourly "$E" $SPAN 3600
  for s in 0.1 0.5 0.9; do q i2-epp-q$s "quantile_over_time($s, ($E)[$RANGE:1h])"; done; q i2-epp-zero-hours "count_over_time((($E) == 0)[$RANGE:1h])"; fi
```
**R11 — node-identity join check (optional; prints counts only, raw UUIDs never leave)**. Needs `$SERVERSCAN_EXPORT` (server-scan rows JSON, fetched read-only by you).
```bash
cat > ~/.ttv/joincheck.py <<'PY'
import collections, json, sys
def get(d, p):
    for k in p.split("."): d = d.get(k) if isinstance(d, dict) else None
    return d
host = lambda s: (s or "").strip().lower().split(".", 1)[0] or None
uu = lambda s: (s or "").strip().lower() or None
r2 = lambda s: "".join(reversed([s[i:i + 2] for i in range(0, len(s), 2)]))
swap = lambda u: "-".join([r2(x) for x in u.split("-")[:3]] + u.split("-")[3:]) if u.count("-") == 4 else u
nodes, raw, cluster = json.load(open(sys.argv[1]))["items"], json.load(open(sys.argv[2])), sys.argv[3]
srv = raw if isinstance(raw, list) else next(raw[k] for k in ("items", "rows", "data") if k in raw)
by_u, by_n, c = collections.defaultdict(list), collections.defaultdict(list), collections.Counter()
for s in srv:
    if uu(get(s, "identity.system_uuid")): by_u[uu(get(s, "identity.system_uuid"))].append(s)
    for n in {host(get(s, "openshift.reported_name")), host(s.get("name"))} - {None}: by_n[n].append(s)
for n in nodes:
    nm, u = host(n["metadata"]["name"]), uu(get(n, "status.nodeInfo.systemUUID"))
    hits = by_n.get(nm, []); c["nodes"] += 1; c[f"name_matches={min(len(hits), 2)}"] += 1
    c["hostname_label_differs"] += host((get(n, "metadata.labels") or {}).get("kubernetes.io/hostname")) != nm
    if len(hits) == 1 and get(hits[0], "openshift.cluster_name") not in (cluster, None): c["name_match_in_other_cluster"] += 1
    if not u: c["uuid_empty"] += 1; continue
    ue, us = by_u.get(u, []), by_u.get(swap(u), [])
    c[f"uuid_exact={min(len(ue), 2)}"] += 1; c[f"uuid_first3_byteswapped={min(len(us), 2)}"] += 1
    if len(hits) == 1 and len(ue or us) == 1: c["name_uuid_agree" if hits[0] is (ue or us)[0] else "name_uuid_DISAGREE"] += 1
print(json.dumps(dict(sorted(c.items()))))
PY
[ -n "$SERVERSCAN_EXPORT" ] && python3 -I ~/.ttv/joincheck.py "$OUT/nodes/nodes.json" "$SERVERSCAN_EXPORT" "$CLUSTER" > "$OUT/nodes/joincheck.json"
```
**R12 — sanitise, summarise, review, paste.** Hostnames, node names, serials, UUIDs and IPs become `h_<10 hex>` (HMAC with your local salt, so the same value hashes the same in every file and cluster); tokens and MACs are removed. `~/.ttv` (salt, token, CA, term lists) is never pasted.
```bash
cat > ~/.ttv/sanitise.py <<'PY'
import hashlib, hmac, pathlib, re, sys
src, dst, salt = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]).read_bytes().strip()
dom = [d.strip(". ") for d in sys.argv[4].split(",") if d.strip()]
terms = {l.strip() for f in sys.argv[5:] for l in open(f) if len(l.strip()) > 2}
H = lambda v: "h_" + hmac.new(salt, v.lower().encode(), hashlib.sha256).hexdigest()[:10]
subs = [(r"eyJ[\w-]{8,}\.[\w-]+\.[\w-]+|sha256~[\w-]{20,}", lambda m: "<TOKEN>"),
        (r"(?i)\b([a-z0-9-]+)((?:\.[a-z0-9-]+)*\.(?:%s))\b" % "|".join(map(re.escape, dom)) if dom else r"(?!x)x", lambda m: H(m[1]) + ".<domain>"),
        (r"\b(?:GPU|MIG)-[0-9a-fA-F-]{36}\b|\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b", lambda m: H(m[0])),
        (r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b", lambda m: "<MAC>"),
        (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", lambda m: H(m[0])), (r"(?i)\b[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){3,7}\b", lambda m: H(m[0]))]
if terms: subs.append((r"(?i)(?<![\w.-])(?:%s)(?![\w-])" % "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), lambda m: H(m[0])))
for p in sorted(x for x in src.rglob("*") if x.is_file()):
    t = p.read_text(errors="replace")
    for rx, fn in subs: t = re.sub(rx, fn, t)
    o = dst / p.relative_to(src); o.parent.mkdir(parents=True, exist_ok=True); o.write_text(t)
print("files still containing an IPv4:", [str(o) for o in dst.rglob("*") if o.is_file() and re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", o.read_text())] or "none")
PY
{ jq -r '.items[]|.metadata.name, (.metadata.labels["kubernetes.io/hostname"]//empty), (.metadata.annotations["machine.openshift.io/machine"]//empty|split("/")|last)' "$OUT/nodes/nodes.json" | cut -d. -f1
  jq -r '.[]?|(.vendor.serialNumber, .name, .host, .consumer, .nodeRef) // empty' "$OUT"/nodes/*.io.json 2>/dev/null; cat ~/.ttv/extra-terms.txt 2>/dev/null; } | sort -u > ~/.ttv/$CLUSTER-terms.txt
python3 -I ~/.ttv/sanitise.py "$OUT" "$OUT.sanitised" ~/.ttv/salt "$BASE_DOMAINS" ~/.ttv/$CLUSTER-terms.txt
cd "$OUT.sanitised" && rm -f nodes/nodes.json pods/pods-min.json owners/objs.jsonl && {
  echo "# $CLUSTER $(date -u +%FT%TZ)"; for f in meta/*.json nodes/*.io.json nodes/joincheck.json serving/objects.jsonl mon/*.json; do echo "## $f"; head -c 4000 "$f"; echo; done
  for f in api/group-*.txt api/crds.tsv nodes/*.tsv nodes/*.txt pods/*.tsv owners/chain-shapes.tsv runai/* serving/*.tsv mon/*.yaml mon/*.csv; do echo "## $f"; head -n 80 "$f"; done
  for f in prom/*.json; do echo "## $f"; jq -r '"\(.status) \(.error // "")", (.data.result[:40][]|"\(.metric|tostring) \(.value[1])")' "$f"; done
  for f in i2/*.json; do echo "## $f series=$(jq '.data.result|length' "$f")"; done; } > SUMMARY.txt 2>&1; wc -l SUMMARY.txt; cd - >/dev/null
```
Before pasting: read `SUMMARY.txt` end to end; search it for your domain, site names, people, IPs and serials; add any leftovers to `~/.ttv/extra-terms.txt` and rerun R12. **Paste back:** `SUMMARY.txt` per cluster. If files can leave the network, also bring the sanitised `i2/*.json` and `prom/i2-*.json` (I2 analysis) and `owners/chains.json`.
