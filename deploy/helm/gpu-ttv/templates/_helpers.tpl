{{/* Names, labels and shared pieces of the engine and collector pod specs. */}}

{{- define "gpu-ttv.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "gpu-ttv.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "gpu-ttv.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "gpu-ttv.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "gpu-ttv.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "gpu-ttv.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: gpu-ttv
{{- end -}}

{{/* Selector labels of one component: pass (dict "ctx" $ "component" "engine"). */}}
{{- define "gpu-ttv.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gpu-ttv.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "gpu-ttv.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "gpu-ttv.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* The image, by digest alone (values.schema.json checks its form). */}}
{{- define "gpu-ttv.image" -}}
{{- printf "%s/%s@%s" .Values.image.registry .Values.image.repository .Values.image.digest -}}
{{- end -}}

{{- define "gpu-ttv.collectorEnabled" -}}
{{- if or .Values.collector.managers.ucs_central .Values.collector.managers.openmanage -}}true{{- end -}}
{{- end -}}

{{/* The policy file: inline values, else the repo's example (files/policy.example.yaml). */}}
{{- define "gpu-ttv.policy" -}}
{{- if .Values.policy -}}
{{- toYaml .Values.policy -}}
{{- else -}}
{{- .Files.Get "files/policy.example.yaml" -}}
{{- end -}}
{{- end -}}

{{- define "gpu-ttv.clusterDir" -}}
/var/run/secrets/ttv/clusters/{{ . }}
{{- end -}}

{{/* clusters.yaml: each entry with token_file and ca_bundle pointing at its mounted Secret. */}}
{{- define "gpu-ttv.clustersFile" -}}
{{- $out := list -}}
{{- range .Values.clusters -}}
{{- $dir := include "gpu-ttv.clusterDir" .name -}}
{{- $gen := dict "token_file" (printf "%s/%s" $dir (.tokenKey | default "token")) -}}
{{- $caKey := ternary .caKey "ca.crt" (hasKey . "caKey") -}}
{{- if $caKey -}}
{{- $_ := set $gen "ca_bundle" (printf "%s/%s" $dir $caKey) -}}
{{- end -}}
{{- if .thanosCaKey -}}
{{- $_ := set $gen "thanos_ca_bundle" (printf "%s/%s" $dir .thanosCaKey) -}}
{{- else if $.Values.caBundle.configMap -}}
{{- $_ := set $gen "thanos_ca_bundle" (printf "/etc/pki/ttv/%s" $.Values.caBundle.key) -}}
{{- end -}}
{{- $out = append $out (merge (omit . "tokenSecret" "tokenKey" "caKey" "thanosCaKey") $gen) -}}
{{- end -}}
{{- toYaml (dict "clusters" $out) -}}
{{- end -}}

{{/* Fail early on what the engine would refuse at startup. */}}
{{- define "gpu-ttv.validate" -}}
{{- $policy := include "gpu-ttv.policy" . | fromYaml -}}
{{- $targets := $policy.target_clusters | default list -}}
{{- range .Values.clusters -}}
{{- if not (has .name $targets) -}}
{{- fail (printf "clusters: %q is not in the policy's target_clusters %v" .name $targets) -}}
{{- end -}}
{{- end -}}
{{- if and (include "gpu-ttv.collectorEnabled" .) (not .Values.database.existingSecret) -}}
{{- fail "collector.managers is set, so database.existingSecret is required: the collector stores its reads in Postgres" -}}
{{- end -}}
{{- end -}}

{{/* Restart a workload when a file it reads only at startup changes. */}}
{{- define "gpu-ttv.engineChecksum" -}}
{{- list (include "gpu-ttv.policy" .) (include "gpu-ttv.clustersFile" .) | join "\n---\n" | sha256sum -}}
{{- end -}}

{{- define "gpu-ttv.collectorChecksum" -}}
{{- toYaml .Values.collector.managers | sha256sum -}}
{{- end -}}

{{/* Environment shared by both workloads: the ledger DSN, the CA bundle and the TTV_* settings. */}}
{{- define "gpu-ttv.commonEnv" -}}
{{- if .Values.database.existingSecret }}
- name: TTV_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.urlKey }}
{{- end }}
{{- if .Values.caBundle.configMap }}
- name: TTV_CA_BUNDLE
  value: /etc/pki/ttv/{{ .Values.caBundle.key }}
{{- end }}
{{- if .Values.pricing.snapshotsConfigMap }}
- name: TTV_PRICING_DIR
  value: /etc/ttv/pricing
{{- end }}
{{- range $name, $value := .Values.settings }}
- name: {{ $name }}
  value: {{ $value | toString | quote }}
{{- end }}
{{- end -}}

{{- define "gpu-ttv.commonMounts" -}}
- name: tmp
  mountPath: /tmp
{{- if .Values.caBundle.configMap }}
- name: ca-bundle
  mountPath: /etc/pki/ttv
  readOnly: true
{{- end }}
{{- if .Values.pricing.snapshotsConfigMap }}
- name: pricing
  mountPath: /etc/ttv/pricing
  readOnly: true
{{- end }}
{{- end -}}

{{- define "gpu-ttv.commonVolumes" -}}
- name: tmp
  emptyDir:
    sizeLimit: 64Mi
{{- if .Values.caBundle.configMap }}
- name: ca-bundle
  configMap:
    name: {{ .Values.caBundle.configMap }}
{{- end }}
{{- if .Values.pricing.snapshotsConfigMap }}
- name: pricing
  configMap:
    name: {{ .Values.pricing.snapshotsConfigMap }}
{{- end }}
{{- end -}}

{{/* Grafana provisioning namespace: the sidecar's, else the release's. */}}
{{- define "gpu-ttv.grafanaNamespace" -}}
{{- .Values.grafana.namespace | default .Release.Namespace -}}
{{- end -}}

{{/* NetworkPolicy egress: DNS plus the configured rules. */}}
{{- define "gpu-ttv.egressRules" -}}
- ports:
    - {port: 53, protocol: UDP}
    - {port: 53, protocol: TCP}
    - {port: 5353, protocol: UDP}
    - {port: 5353, protocol: TCP}
{{ toYaml .Values.networkPolicy.egress }}
{{- end -}}
