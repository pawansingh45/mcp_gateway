{{- define "mcp.secretName" -}}
{{- .Values.existingSecret | default (printf "%s-secrets" .Release.Name) -}}
{{- end -}}

{{- define "mcp.labels" -}}
app.kubernetes.io/name: mcp-platform
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{- end -}}

{{- define "mcp.selector" -}}
app.kubernetes.io/name: mcp-platform
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* image reference: registry/repository:tag (tag defaults to appVersion) */}}
{{- define "mcp.image" -}}
{{- $reg := .root.Values.images.registry -}}
{{- printf "%s%s:%s" (ternary (printf "%s/" $reg) "" (ne $reg "")) .img.repository (.img.tag | default .root.Chart.AppVersion) -}}
{{- end -}}

{{- define "mcp.pullSecrets" -}}
{{- with .Values.images.imagePullSecrets }}
imagePullSecrets:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "mcp.storageClass" -}}
{{- with .Values.storageClass }}
storageClassName: {{ . | quote }}
{{- end }}
{{- end -}}

{{/* keep a previously generated secret value, otherwise use the generated one */}}
{{- define "mcp.keep" -}}
{{- $v := (get .old .key | default "" | b64dec) -}}
{{- if $v }}{{ $v }}{{ else }}{{ .gen }}{{ end -}}
{{- end -}}

{{- define "mcp.storeUrl" -}}
{{ .Values.ingress.scheme }}://{{ .Values.ingress.storeHost }}
{{- end -}}

{{- define "mcp.gatewayUrl" -}}
{{ .Values.ingress.scheme }}://{{ .Values.ingress.gatewayHost }}
{{- end -}}

{{/* hardened container context shared by the Python services */}}
{{- define "mcp.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  runAsNonRoot: true
  capabilities: { drop: ["ALL"] }
{{- end -}}

{{- define "mcp.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile: { type: RuntimeDefault }
{{- end -}}

{{/* NetworkPolicy ingress rule: allow from pods of this release with the given component label */}}
{{- define "mcp.np.from" -}}
- from:
    - podSelector:
        matchLabels:
          app.kubernetes.io/instance: {{ .root.Release.Name }}
          app.kubernetes.io/component: {{ .from }}
{{- end -}}
