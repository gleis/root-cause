{{- define "rootcause.name" -}}
rootcause
{{- end -}}

{{- define "rootcause.fullname" -}}
{{ .Release.Name }}-rootcause
{{- end -}}

{{- define "rootcause.labels" -}}
app.kubernetes.io/name: {{ include "rootcause.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "rootcause.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ .Values.serviceAccount.name | default (include "rootcause.fullname" .) }}
{{- else -}}
{{ .Values.serviceAccount.name | default "default" }}
{{- end -}}
{{- end -}}
