{{/* Names, labels, the policy and clusters files, and render-time checks. */}}

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
{{- range $k := list "teams" "stage_objectives" -}}
{{- if hasKey (get $policy $k | default dict) "PRE_PROVISIONING" -}}
{{- fail (printf "policy.%s: the DC stage (PRE_PROVISIONING) was removed (ADR-0008); delete it" $k) -}}
{{- end -}}
{{- end -}}
{{- if hasKey .Values "collector" -}}
{{- fail "collector: the collector was removed in chart 0.2.0 (ADR-0008); delete this section from your values" -}}
{{- end -}}
{{- end -}}

{{/* Restart the engine when a file it reads only at startup changes. */}}
{{- define "gpu-ttv.engineChecksum" -}}
{{- list (include "gpu-ttv.policy" .) (include "gpu-ttv.clustersFile" .) | join "\n---\n" | sha256sum -}}
{{- end -}}

{{/* Grafana provisioning namespace: the sidecar's, else the release's. */}}
{{- define "gpu-ttv.grafanaNamespace" -}}
{{- .Values.grafana.namespace | default .Release.Namespace -}}
{{- end -}}
