"""The Grafana dashboard and the alert rules must only use metrics (and labels) the service exports,
and the committed dashboard JSON must match its generator."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import yaml
from prometheus_client import Counter, Histogram

from chat_service import metrics

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "ops/grafana/dashboards/agencyos-assistant.json"


def _exported() -> dict[str, set[str]]:
    """Series name → label names, for every aos_* metric."""
    out: dict[str, set[str]] = {}
    for obj in vars(metrics).values():
        if isinstance(obj, Counter):
            out[f"{obj._name}_total"] = set(obj._labelnames)
        elif isinstance(obj, Histogram):
            for suffix in ("_bucket", "_sum", "_count"):
                out[obj._name + suffix] = set(obj._labelnames) | ({"le"} if suffix == "_bucket" else set())
    return out


def _check(expr: str, exported: dict[str, set[str]]) -> list[str]:
    problems = []
    names = re.findall(r"\b(aos_[a-z_]+)", expr)
    for name in names:
        if name not in exported:
            problems.append(f"unknown metric {name}")
    allowed = set().union(*(exported.get(n, set()) for n in names)) | {"instance", "job"}
    used = set()
    for group in re.findall(r"\bby \(([^)]*)\)", expr):
        used |= {x.strip() for x in group.split(",") if x.strip()}
    used |= set(re.findall(r"[{,]\s*([a-z_]+)\s*=~?", expr))
    if names:
        problems += [f"label {lbl!r} not on {names}" for lbl in used - allowed]
    return problems


def _dashboard_exprs() -> list[tuple[str, str]]:
    d = json.loads(DASHBOARD.read_text())
    return [(p["title"], t["expr"]) for p in d["panels"] for t in p.get("targets", [])]


def test_dashboard_queries_use_exported_metrics_and_labels():
    exported = _exported()
    exprs = _dashboard_exprs()
    assert len(exprs) >= 20
    problems = {title: probs for title, expr in exprs if (probs := _check(expr, exported))}
    assert problems == {}
    used = {n for _, e in exprs for n in re.findall(r"\b(aos_[a-z_]+)", e)}
    # every metric the service exports is on the dashboard somewhere
    families = {re.sub(r"_(total|bucket|sum|count)$", "", n) for n in exported}
    assert families <= {re.sub(r"_(total|bucket|sum|count)$", "", n) for n in used}


def test_alert_rules_use_exported_metrics_and_labels():
    exported = _exported()
    rules = yaml.safe_load((ROOT / "ops/prometheus/alerts.yml").read_text())
    exprs = [(r["alert"], r["expr"]) for g in rules["groups"] for r in g["rules"]]
    assert {a for a, _ in exprs} >= {"AosAvailabilityBudgetBurn", "AosChatReplyErrors", "AosInstanceDown"}
    assert {a: p for a, e in exprs if (p := _check(e, exported))} == {}


def test_dashboard_json_matches_generator_and_layout_is_valid():
    spec = importlib.util.spec_from_file_location("build_dashboard", ROOT / "ops/grafana/build_dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    built = mod.build()
    assert json.loads(DASHBOARD.read_text()) == built, "run: python ops/grafana/build_dashboard.py"
    ids = [p["id"] for p in built["panels"]]
    assert len(ids) == len(set(ids))
    for p in built["panels"]:
        pos = p["gridPos"]
        assert pos["x"] + pos["w"] <= 24, p["title"]
