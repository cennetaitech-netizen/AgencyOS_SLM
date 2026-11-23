# Deploying on Kubernetes (Helm)

`deploy/helm/agencyos-assistant` installs the chat service:

| Part | What it is |
|---|---|
| **API** | Deployment, 2+ pods with 4 workers each. Autoscaled on CPU (2–10), spread across nodes, at least one kept up during node maintenance. |
| **Jobs** | Exactly one pod (`python -m chat_service.jobs`): billing cycles, dunning, retention, canary checks. It's replaced, never rolled, so two copies never run at once. |
| **Migrations** | `alembic upgrade head` as a Job before every install and upgrade. If it fails, the release fails and the running version keeps serving. |
| **Optional** | Ingress tuned for streaming, Prometheus Operator ServiceMonitor and alert rules, NetworkPolicy. |

Postgres, Redis and the model host are **not** in the chart. Use managed Postgres (point-in-time recovery), Redis without persistence, and the model host from [ops/MODEL_HOST.md](../../ops/MODEL_HOST.md).

Every pod runs as the image's non-root user (10001), with a read-only root filesystem, no Linux capabilities and no service account token. `/tmp` is the only writable path.

CI (`.github/workflows/helm.yml`) lints the chart, validates the rendered manifests, then installs it on a throwaway kind cluster. It runs `helm test` and the end-to-end chat smoke test, upgrades the release (migrations run again), and checks that nothing restarted.

## 1. Image
Build and push the image from the repository root:
```bash
docker build -t registry.example.com/agencyos-assistant:0.1.0 .
```
```bash
docker push registry.example.com/agencyos-assistant:0.1.0
```

**Billing tokenizer.** In production, the app needs the pinned billing tokenizer file (`CHAT_BILLING_TOKENIZER_PATH`). It's several MB, too big for a ConfigMap, so bake it into a small derived image:
```dockerfile
FROM registry.example.com/agencyos-assistant:0.1.0
COPY tokenizer.json /srv/tokenizer.json
```
Alternatively, mount a read-only volume with `extraVolumes` and `extraVolumeMounts`.

## 2. Secret
Create the Secret before the first install, because the migration Job reads it. Best practice is to sync it from your secrets manager (External Secrets, Sealed Secrets and similar tools). The by-hand equivalent:
```bash
kubectl create secret generic agencyos-assistant-secrets \
  --from-literal=CHAT_DATABASE_URL='postgresql+asyncpg://USER:PASSWORD@HOST:5432/chat' \
  --from-literal=CHAT_REDIS_URL='redis://HOST:6379/0' \
  --from-literal=CHAT_METRICS_TOKEN="$(openssl rand -hex 24)" \
  --from-literal=CHAT_DATA_ENCRYPTION_KEYS='["FERNET_KEY"]' \
  --from-literal=CHAT_MODEL_API_KEY='...'
```
- Make a Fernet key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- To rotate keys, put the new key first and keep the old ones after it.
- Add `CHAT_MODEL_FALLBACK_API_KEY` if you use a fallback host.
- Add `CHAT_PAYMENT_WEBHOOK_SECRET` only with an online payment provider. Manual billing doesn't need it.

## 3. Values
Start from [examples/values-production.yaml](examples/values-production.yaml) and set your image, hosts and origins. Every setting in [.env.example](../../.env.example) can go under `config:` as `CHAT_*: value`. Lists are JSON, for example `CHAT_CORS_ALLOWED_ORIGINS: '["https://app.agencyos.example"]'`.

With `environment: production`, the app refuses to start if a setting is unsafe: dev sign-in, the mock product API, dev payments, a missing tokenizer, encryption keys, metrics token or CORS list. The chart also refuses to render without a Secret.

## 4. Install and upgrade
```bash
helm upgrade --install aos deploy/helm/agencyos-assistant -n agencyos --create-namespace \
  -f my-values.yaml --wait --timeout 10m
```
```bash
helm test aos -n agencyos
```
- **Upgrades:** run the same `helm upgrade --install` command. Migrations run first, then pods roll one at a time with no downtime. Each API pod gets 120 s to finish open replies (`api.terminationGracePeriodSeconds`).
- **A failed migration:** the release is marked failed and the old pods keep serving. Read why with `kubectl -n agencyos logs job/aos-agencyos-assistant-migrate`.
- **Rollback:** `helm rollback aos`. This rolls back the pods, not the database schema. The previous release must work with the newer schema. So far every migration only adds tables or columns, which keeps that true. Keep it that way: remove a column one release after the code stops using it.

## 5. Ingress (streaming)
Replies stream as server-sent events, so the proxy must not buffer them, and its read timeout must be longer than the longest reply. The default annotations do this for ingress-nginx: `proxy-buffering: "off"` and 300 s timeouts. Other controllers need their own equivalents. For example, on AWS ALB raise the idle timeout to 300 s.

## 6. Monitoring
- `metrics.serviceMonitor.enabled: true` scrapes `/metrics` with the token from the Secret. Prometheus labels the target `job="agencyos-assistant"`, which the alerts and the dashboard expect.
- `metrics.prometheusRule.enabled: true` installs the 10 alert rules from [ops/prometheus/alerts.yml](../../ops/prometheus/alerts.yml). The chart keeps a copy in `files/alerts.yml`, and a test fails if the two differ.
- Grafana: import [ops/grafana/dashboards/agencyos-assistant.json](../../ops/grafana/dashboards/agencyos-assistant.json).
- Your Prometheus may select rules and monitors by label (for example `release: kube-prometheus-stack`). If so, add that label under `metrics.*.labels`.

## 7. Network policy
`networkPolicy.enabled: true` lets only the namespaces listed in `networkPolicy.ingressFrom` reach the API pods. Typically that's the ingress controller's and Prometheus' namespaces. Outbound traffic isn't restricted, because the pods need Postgres, Redis, the model host and the product API, and their addresses differ per deployment.

## Try it locally (kind)
The CI install, step by step. It needs Docker, kind, kubectl and helm.
```bash
kind create cluster --name aos
```
```bash
docker build -t agencyos-assistant:ci . && kind load docker-image agencyos-assistant:ci --name aos
```
```bash
kubectl create configmap fake-model-host --from-file=ops/fake_model_host.py --from-file=ops/prefix_cache.py
```
```bash
kubectl apply -f deploy/kind/stand-ins.yaml && kubectl wait --for=condition=available deploy --all --timeout=240s
```
```bash
helm install aos deploy/helm/agencyos-assistant -f deploy/kind/values-ci.yaml --wait --timeout 6m
```
```bash
kubectl port-forward svc/aos-agencyos-assistant 8090:80
```
Then, in another terminal:
```bash
python3 ops/smoke_stack.py --api http://localhost:8090 --metrics-token dev-metrics-token
```
