{{- define "aos.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "aos.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "aos.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ include "aos.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Selector labels; call with (dict "ctx" $ "component" "api") */}}
{{- define "aos.selector" -}}
app.kubernetes.io/name: {{ include "aos.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "aos.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "aos.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "aos.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "aos.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- include "aos.fullname" . -}}
{{- end -}}
{{- end -}}

{{/* Settings for every container. Inline rather than a ConfigMap, so the pre-install migration hook can use them
     and any change rolls the pods. */}}
{{- define "aos.env" -}}
- name: CHAT_ENV
  value: {{ .Values.environment | quote }}
{{- range $k, $v := .Values.config }}
- name: {{ $k }}
  value: {{ $v | toString | quote }}
{{- end }}
{{- end -}}

{{- define "aos.envFrom" -}}
{{- if or .Values.secrets.existingSecret .Values.secrets.create -}}
envFrom:
  - secretRef:
      name: {{ include "aos.secretName" . }}
{{- end -}}
{{- end -}}

{{/* /tmp is the only writable path: the root filesystem is read-only, and the image keeps per-worker metrics
     files in /tmp/aos-metrics (PROMETHEUS_MULTIPROC_DIR). */}}
{{- define "aos.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- with .Values.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "aos.volumes" -}}
- name: tmp
  emptyDir: {sizeLimit: 256Mi}
{{- with .Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end -}}
