"""Static checks on the Helm chart (deploy/helm). Rendering and a real install run in .github/workflows/helm.yml."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

from chat_service.config import Settings, production_problems

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/helm/agencyos-assistant"
CI_VALUES = ROOT / "deploy/kind/values-ci.yaml"
PROD_VALUES = ROOT / "deploy/helm/examples/values-production.yaml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_alert_rules_copy_matches_ops():
    assert (CHART / "files/alerts.yml").read_text() == (ROOT / "ops/prometheus/alerts.yml").read_text(), (
        "copy ops/prometheus/alerts.yml to deploy/helm/agencyos-assistant/files/alerts.yml"
    )


def test_chart_version_follows_the_app():
    chart = _load(CHART / "Chart.yaml")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert chart["appVersion"] == project["version"]


def test_every_setting_named_in_values_exists():
    """A misspelt CHAT_* name would be silently ignored by the app, so check them against Settings."""
    names: set[str] = set()
    for path in (CHART / "values.yaml", CI_VALUES, PROD_VALUES):
        names |= set(re.findall(r"\bCHAT_[A-Z0-9_]+", path.read_text()))  # includes commented-out examples
    names.discard("CHAT_ENV")  # set from .Values.environment
    assert len(names) >= 10
    unknown = sorted(n for n in names if n.removeprefix("CHAT_").lower() not in Settings.model_fields)
    assert unknown == []


def test_production_example_passes_the_app_guard_once_secrets_are_added():
    values = _load(PROD_VALUES)
    assert values["environment"] == "production" and values["secrets"]["existingSecret"]
    env = {k.removeprefix("CHAT_").lower(): v for k, v in values["config"].items()}
    env["cors_allowed_origins"] = yaml.safe_load(env["cors_allowed_origins"])  # JSON in the env var
    secret = dict(
        database_url="postgresql+asyncpg://u:p@db.internal/chat",
        redis_url="redis://redis.internal:6379/0",
        metrics_token="m",
        data_encryption_keys=["k"],
    )
    assert production_problems(Settings(**env, **secret)) == []


def test_ci_values_use_the_stand_in_services():
    values = _load(CI_VALUES)
    stand_ins = {d["metadata"]["name"] for d in yaml.safe_load_all((ROOT / "deploy/kind/stand-ins.yaml").read_text())}
    assert {"postgres", "redis", "model"} <= stand_ins
    urls = " ".join(map(str, [*values["config"].values(), *values["secrets"]["values"].values()]))
    for host in ("postgres:5432", "redis:6379", "model:8003"):
        assert host in urls
    assert values["environment"] != "production" and values["image"]["pullPolicy"] == "Never"


def test_jobs_never_run_twice_and_api_survives_disruption():
    jobs = (CHART / "templates/deployment-jobs.yaml").read_text()
    assert "replicas: 1" in jobs and "type: Recreate" in jobs
    defaults = _load(CHART / "values.yaml")
    assert defaults["api"]["podDisruptionBudget"]["minAvailable"] >= 1
    assert defaults["api"]["autoscaling"]["minReplicas"] >= 2
    assert defaults["securityContext"]["readOnlyRootFilesystem"] is True
    assert defaults["podSecurityContext"]["runAsUser"] == 10001  # the image's app user (Dockerfile)
    assert defaults["environment"] == "production"
