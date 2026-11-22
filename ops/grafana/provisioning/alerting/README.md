# Alerting

Alert rules live in Prometheus (`ops/prometheus/alerts.yml`) and are routed by Alertmanager, so this folder is
intentionally empty. Grafana needs it to exist and only reads `*.yaml` files here.
