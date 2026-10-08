{{/*
Expand the name of the chart.
*/}}
{{- define "modus.name" -}}
{{- default .Chart.Name .Values.global.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "modus.fullname" -}}
{{- if .Values.global.fullnameOverride }}
{{- .Values.global.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.global.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Chart label
*/}}
{{- define "modus.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "modus.labels" -}}
helm.sh/chart: {{ include "modus.chart" . }}
{{ include "modus.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "modus.selectorLabels" -}}
app.kubernetes.io/name: {{ include "modus.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Orchestrator image reference. global.imageRegistry, when set, is prefixed to the repository.
*/}}
{{- define "modus.image" -}}
{{- if .Values.global.imageRegistry -}}
{{- printf "%s/%s:%s" (trimSuffix "/" .Values.global.imageRegistry) .Values.orchestrator.image.repository (toString .Values.orchestrator.image.tag) -}}
{{- else -}}
{{- printf "%s:%s" .Values.orchestrator.image.repository (toString .Values.orchestrator.image.tag) -}}
{{- end -}}
{{- end }}

{{/*
Name of the Secret created by the chart (secrets.create=true).
*/}}
{{- define "modus.secretName" -}}
{{- printf "%s-secrets" (include "modus.fullname" .) -}}
{{- end }}

{{/*
valueFrom secretKeyRef blocks. When secrets.create=true all three point at the
chart-created Secret, otherwise at the existingSecret named in values.
*/}}
{{- define "modus.dbUrlEnv" -}}
- name: MODUS_DATABASE_URL
  valueFrom:
    secretKeyRef:
      {{- if .Values.secrets.create }}
      name: {{ include "modus.secretName" . }}
      key: database-url
      {{- else }}
      name: {{ .Values.database.existingSecret | required "database.existingSecret is required (or set secrets.create=true)" }}
      key: {{ .Values.database.secretKey }}
      {{- end }}
{{- end }}

{{- define "modus.masterKeyEnv" -}}
- name: MODUS_MASTER_API_KEY
  valueFrom:
    secretKeyRef:
      {{- if .Values.secrets.create }}
      name: {{ include "modus.secretName" . }}
      key: master-api-key
      {{- else }}
      name: {{ .Values.auth.existingSecret | required "auth.existingSecret is required (or set secrets.create=true)" }}
      key: {{ .Values.auth.secretKey }}
      {{- end }}
{{- end }}

{{- define "modus.jwtSecretEnv" -}}
- name: MODUS_JWT_SECRET
  valueFrom:
    secretKeyRef:
      {{- if .Values.secrets.create }}
      name: {{ include "modus.secretName" . }}
      key: jwt-secret
      {{- else }}
      name: {{ .Values.auth.jwt.existingSecret | required "auth.jwt.existingSecret is required when authMode=jwt (or set secrets.create=true)" }}
      key: {{ .Values.auth.jwt.secretKey }}
      {{- end }}
{{- end }}
