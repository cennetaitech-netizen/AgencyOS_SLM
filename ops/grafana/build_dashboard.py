"""Builds the Grafana dashboard for the assistant (Phase 12) from the metrics in ``chat_service/metrics.py``.

  python ops/grafana/build_dashboard.py      # rewrites ops/grafana/dashboards/agencyos-assistant.json

Edit the panels here rather than in the JSON, so queries stay reviewable. Thresholds match the SLOs and
alert rules (docs/LAUNCH_READINESS.md §2, ops/prometheus/alerts.yml). ``tests/test_dashboard.py`` checks
that every query uses only metrics the service exports.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards" / "agencyos-assistant.json"
DS = {"type": "prometheus", "uid": "${datasource}"}
SEL = 'instance=~"$instance"'  # every query is filtered by the instance picker
WIN = "$__rate_interval"

GREEN, AMBER, RED = "green", "#EAB839", "red"


class Layout:
    """Places panels left to right on Grafana's 24-column grid."""

    def __init__(self) -> None:
        self.x = self.y = self.row_h = 0
        self.panels: list[dict] = []
        self.next_id = 1

    def row(self, title: str) -> None:
        if self.x:
            self.y += self.row_h
        self.panels.append(
            {
                "type": "row",
                "title": title,
                "collapsed": False,
                "id": self._id(),
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y},
                "panels": [],
            }
        )
        self.y += 1
        self.x = self.row_h = 0

    def add(self, panel: dict, w: int, h: int) -> None:
        if self.x + w > 24:
            self.y += self.row_h
            self.x = self.row_h = 0
        panel["id"] = self._id()
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id - 1


def hq(q: float, metric: str, by: str = "le", window: str = WIN) -> str:
    """histogram_quantile over a histogram's buckets, filtered by instance."""
    return f"histogram_quantile({q}, sum by ({by}) (rate({metric}_bucket{{{SEL}}}[{window}])))"


def ratio(num: str, den: str, window: str = WIN) -> str:
    return f"sum(rate({num}{{{SEL}}}[{window}])) / sum(rate({den}{{{SEL}}}[{window}]))"


def uncached_ratio(window: str = WIN) -> str:
    """Model-host tokens that had to be computed (not served from the prefix cache) per billable token."""
    host = f"sum(rate(aos_chat_host_tokens_total{{{SEL}}}[{window}]))"
    cached = f"sum(rate(aos_chat_host_cached_prompt_tokens_total{{{SEL}}}[{window}]))"
    return f"({host} - {cached}) / sum(rate(aos_chat_billable_tokens_total{{{SEL}}}[{window}]))"


def cache_hit(window: str = WIN) -> str:
    cached = f"sum(rate(aos_chat_host_cached_prompt_tokens_total{{{SEL}}}[{window}]))"
    return f'{cached} / sum(rate(aos_chat_host_tokens_total{{kind="prompt",{SEL}}}[{window}]))'


def target(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict:
    t = {"datasource": DS, "expr": expr, "legendFormat": legend or "__auto", "refId": ref, "range": not instant}
    if instant:
        t["instant"] = True
    return t


def steps(*pairs: tuple[float | None, str]) -> dict:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in pairs]}


def stat(title: str, expr: str, unit: str, thresholds: dict, description: str, decimals: int | None = None) -> dict:
    defaults: dict = {"unit": unit, "thresholds": thresholds, "color": {"mode": "thresholds"}, "noValue": "no traffic"}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": DS,
        "targets": [target(expr)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background",
            "graphMode": "area",
            "textMode": "value",
            "justifyMode": "center",
        },
    }


def ts(
    title: str,
    targets: list[dict],
    unit: str,
    description: str,
    *,
    stack: bool = False,
    threshold: float | None = None,
    bars: bool = False,
    overrides: list | None = None,
    no_value: str | None = None,
    soft_max: float | None = None,
) -> dict:
    custom = {
        "drawStyle": "bars" if bars else "line",
        "lineWidth": 2,
        "fillOpacity": 60 if bars else 10,
        "showPoints": "never",
        "spanNulls": True,
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
    }
    if soft_max is not None:
        custom["axisSoftMax"] = soft_max  # keeps a sensible scale when every value is 0
    thresholds = steps((None, GREEN))
    if threshold is not None:
        custom["thresholdsStyle"] = {"mode": "dashed"}
        thresholds = steps((None, "transparent"), (threshold, RED))
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": DS,
        "targets": targets,
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": custom, "thresholds": thresholds, "min": 0}
            | ({"noValue": no_value} if no_value else {}),
            "overrides": overrides or [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def color(name: str, c: str) -> dict:
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}],
    }


def build() -> dict:
    g = Layout()

    # ---------------------------------------------------------------- SLO headline
    g.row("Launch SLOs (red = breaching; each matches an alert rule)")
    g.add(
        stat(
            "Availability (30 min)",
            f'1 - (sum(rate(aos_http_requests_total{{status=~"5..",{SEL}}}[30m])) or vector(0))'
            f" / sum(rate(aos_http_requests_total{{{SEL}}}[30m]))",
            "percentunit",
            steps((None, RED), (0.995, GREEN)),
            "Share of API requests that didn't fail with a 5xx. SLO ≥ 99.5%. Alert: AosAvailabilityBudgetBurn.",
            decimals=2,
        ),
        4,
        4,
    )
    g.add(
        stat(
            "Failed replies (15 min)",
            f'(sum(rate(aos_chat_messages_total{{status=~"model_error|internal_error",{SEL}}}[15m])) or vector(0))'
            f" / sum(rate(aos_chat_messages_total{{{SEL}}}[15m]))",
            "percentunit",
            steps((None, GREEN), (0.01, AMBER), (0.02, RED)),
            "Assistant replies that failed (never charged). SLO < 2%. Alert: AosChatReplyErrors.",
            decimals=2,
        ),
        4,
        4,
    )
    g.add(
        stat(
            "p95 first token (10 min)",
            hq(0.95, "aos_chat_time_to_first_token_seconds", window="10m"),
            "s",
            steps((None, GREEN), (1.0, AMBER), (1.5, RED)),
            "How long users wait before the reply starts. SLO < 1.5 s. Alert: AosTimeToFirstTokenSlow.",
            decimals=2,
        ),
        4,
        4,
    )
    g.add(
        stat(
            "Unverified figures (1 h)",
            f"(sum(rate(aos_chat_ungrounded_answers_total{{{SEL}}}[1h])) or vector(0))"
            f' / sum(rate(aos_chat_messages_total{{status="ok",{SEL}}}[1h]))',
            "percentunit",
            steps((None, GREEN), (0.03, AMBER), (0.05, RED)),
            "Answers with numbers not found in tool data. Target < 5%. Alert: AosUngroundedAnswersHigh.",
            decimals=1,
        ),
        4,
        4,
    )
    g.add(
        stat(
            "Uncached cost ratio (1 h)",
            uncached_ratio("1h"),
            "x",
            steps((None, GREEN), (15, AMBER), (20, RED)),
            "Model-host tokens the host had to compute (not served from its prefix cache) per billable token. "
            "Above 20× the margin is at risk. Alert: AosOverheadRatioHigh.",
            decimals=1,
        ),
        4,
        4,
    )
    g.add(
        stat(
            "Instances up",
            'sum(up{job="agencyos-assistant",' + SEL + "})",
            "none",
            steps((None, RED), (2, GREEN)),
            "API instances Prometheus can scrape. Fewer than 2 means no redundancy. Alert: AosInstanceDown.",
        ),
        4,
        4,
    )

    # ---------------------------------------------------------------- traffic & latency
    g.row("Chat traffic and latency")
    g.add(
        ts(
            "Messages per second by outcome",
            [target(f"sum by (status) (rate(aos_chat_messages_total{{{SEL}}}[{WIN}]))", "{{status}}")],
            "reqps",
            "Every assistant message, by how it ended. Only 'ok' is charged.",
            stack=True,
            overrides=[color("ok", GREEN), color("model_error", RED), color("internal_error", "dark-red")],
        ),
        12,
        8,
    )
    g.add(
        ts(
            "First token and full reply time",
            [
                target(hq(0.5, "aos_chat_time_to_first_token_seconds"), "first token p50", "A"),
                target(hq(0.95, "aos_chat_time_to_first_token_seconds"), "first token p95", "B"),
                target(hq(0.5, "aos_chat_message_seconds"), "full reply p50", "C"),
                target(hq(0.95, "aos_chat_message_seconds"), "full reply p95", "D"),
            ],
            "s",
            "Dashed line: the 1.5 s first-token SLO.",
            threshold=1.5,
        ),
        12,
        8,
    )
    g.add(
        ts(
            "Errors shown to users, by code",
            [target(f"sum by (code) (rate(aos_chat_errors_total{{{SEL}}}[{WIN}]))", "{{code}}")],
            "reqps",
            "Codes users see. Failed replies all show as internal_error (the cause, model host or our "
            "service, is split in 'Messages per second by outcome'). Spikes in insufficient_tokens or "
            "cap_reached are sales signals; rate_limited means a user sent more than the per-minute limit.",
            stack=True,
            bars=True,
        ),
        8,
        8,
    )
    g.add(
        ts(
            "HTTP 5xx by route",
            [target(f'sum by (route) (rate(aos_http_requests_total{{status=~"5..",{SEL}}}[{WIN}]))', "{{route}}")],
            "reqps",
            "Server errors per API route. Any sustained line here needs a look (runbook rb-api-errors). "
            "Failed chat replies don't show here: the stream has already started, so they are counted as "
            "messages with an error outcome.",
            stack=True,
            no_value="No server errors",
        ),
        8,
        8,
    )
    g.add(
        ts(
            "p95 API time by route (non-streaming)",
            [
                target(
                    f"histogram_quantile(0.95, sum by (le, route) (rate(aos_http_request_duration_seconds_bucket"
                    f'{{route!~"/metrics|/readyz|/healthz",{SEL}}}[{WIN}])))',
                    "{{route}}",
                )
            ],
            "s",
            "Wallet, conversations, billing and admin calls (health checks and /metrics left out). "
            "Streaming replies are measured above.",
        ),
        8,
        8,
    )

    # ---------------------------------------------------------------- tokens & margin
    g.row("Tokens and margin")
    g.add(
        ts(
            "Billable vs model-host tokens per second",
            [
                target(
                    f"sum by (billing_mode) (rate(aos_chat_billable_tokens_total{{{SEL}}}[{WIN}]))",
                    "billable ({{billing_mode}})",
                    "A",
                ),
                target(f"sum by (kind) (rate(aos_chat_host_tokens_total{{{SEL}}}[{WIN}]))", "host {{kind}}", "B"),
                target(
                    f"sum(rate(aos_chat_host_cached_prompt_tokens_total{{{SEL}}}[{WIN}]))", "host prompt (cached)", "C"
                ),
            ],
            "short",
            "Billable = what clients pay for (or would, in shadow mode). Host = what the model "
            "processed; 'host prompt (cached)' is the part served from its prefix cache.",
        ),
        6,
        8,
    )
    g.add(
        ts(
            "Hidden cost ratio",
            [
                target(ratio("aos_chat_host_tokens_total", "aos_chat_billable_tokens_total"), "all host tokens", "A"),
                target(uncached_ratio(), "uncached (drives cost)", "B"),
            ],
            "x",
            "Host tokens per billable token. Dashed line: 20×, the margin alert on the uncached ratio. "
            "Rising means prompts or tool results grew, or the prefix cache stopped hitting (runbook rb-margin).",
            threshold=20,
        ),
        6,
        8,
    )
    g.add(
        ts(
            "Prompt cache hit rate",
            [target(cache_hit(), "cached ÷ prompt tokens")],
            "percentunit",
            "Share of prompt tokens served from the model host's prefix cache. Expect most of it: the system "
            "prompt and tool schemas are identical for everyone. A drop means caching is off, a prompt change "
            "broke the shared prefix, or traffic isn't routed to warm replicas. "
            "Alert below 50%: AosPromptCacheHitLow.",
            soft_max=1,
            no_value="Host doesn't report cache hits",
        ),
        6,
        8,
    )
    g.add(
        ts(
            "Host tokens by model",
            [target(f"sum by (model) (rate(aos_chat_host_tokens_total{{{SEL}}}[{WIN}]))", "{{model}}")],
            "short",
            "Shows the split during a canary rollout of a new model version.",
            stack=True,
        ),
        6,
        8,
    )

    # ---------------------------------------------------------------- tools & actions
    g.row("Tools and chat actions")
    g.add(
        ts(
            "Tool calls by tool",
            [target(f"sum by (tool) (rate(aos_chat_tool_calls_total{{{SEL}}}[{WIN}]))", "{{tool}}")],
            "reqps",
            "Reads from AgencyOS data and built-in calculators.",
            stack=True,
        ),
        8,
        8,
    )
    g.add(
        ts(
            "Tool call failure rate",
            [
                target(
                    # "or 0 * …" draws a 0% line for tools with calls but no failures, instead of no data
                    f'(sum by (tool) (rate(aos_chat_tool_calls_total{{ok="false",{SEL}}}[{WIN}]))'
                    f" or 0 * sum by (tool) (rate(aos_chat_tool_calls_total{{{SEL}}}[{WIN}])))"
                    f" / sum by (tool) (rate(aos_chat_tool_calls_total{{{SEL}}}[{WIN}]))",
                    "{{tool}}",
                )
            ],
            "percentunit",
            "Share of calls to each tool that failed (product API down, bad arguments).",
            soft_max=0.05,
        ),
        8,
        8,
    )
    g.add(
        ts(
            "Chat actions by outcome",
            [target(f"sum by (outcome) (increase(aos_chat_actions_total{{{SEL}}}[{WIN}]))", "{{outcome}}")],
            "short",
            "Campaign changes proposed in chat and what happened to them: proposed, executed, cancelled, "
            "rejected (by a safety rule), rejected_by_approver, expired, failed. 'failed' pages on-call "
            "above 3 in 30 min (runbook rb-actions).",
            stack=True,
            bars=True,
            no_value="No chat actions",
            overrides=[
                color("proposed", "light-blue"),
                color("executed", GREEN),
                color("cancelled", "#8F8F8F"),
                color("rejected", "orange"),
                color("rejected_by_approver", "purple"),
                color("expired", "#C4C4C4"),
                color("failed", RED),
            ],
        ),
        8,
        8,
    )

    # ---------------------------------------------------------------- resilience
    g.row("Resilience")
    g.add(
        ts(
            "Replies served by the fallback model host",
            [target(f"sum(rate(aos_model_fallback_total{{{SEL}}}[{WIN}]))", "fallback")],
            "reqps",
            "Non-zero means the primary model host is failing before the first token (runbook rb-model-host).",
            no_value="Not in use",
        ),
        8,
        7,
    )
    g.add(
        ts(
            "Requests allowed without limits (Redis unreachable)",
            [target(f"sum by (kind) (increase(aos_limiter_fail_open_total{{{SEL}}}[{WIN}]))", "{{kind}}")],
            "short",
            "Rate and concurrency limits fail open when Redis is down. Billing stays safe (runbook rb-redis).",
            bars=True,
            no_value="None: Redis reachable",
        ),
        8,
        7,
    )
    g.add(
        {
            "type": "table",
            "title": "Firing alerts",
            "datasource": DS,
            "description": "Alerts from ops/prometheus/alerts.yml that are firing now.",
            "targets": [target('ALERTS{alertstate="firing"}', "", "A", instant=True)],
            "transformations": [
                {"id": "labelsToFields", "options": {}},
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {
                            "Time": True,
                            "Value": True,
                            "__name__": True,
                            "alertstate": True,
                            "job": True,
                            "instance": True,
                        },
                        "indexByName": {"alertname": 0, "severity": 1},
                    },
                },
            ],
            "fieldConfig": {"defaults": {"noValue": "None firing"}, "overrides": []},
            "options": {"showHeader": True, "cellHeight": "sm"},
        },
        8,
        7,
    )

    return {
        "uid": "agencyos-assistant",
        "title": "AgencyOS Assistant",
        "description": "Launch SLOs, traffic, tokens and margin, tools and actions, resilience.",
        "tags": ["agencyos", "assistant"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "refresh": "30s",
        "schemaVersion": 39,
        "version": 1,
        "time": {"from": "now-1h", "to": "now"},
        "templating": {
            "list": [
                {
                    "type": "datasource",
                    "name": "datasource",
                    "label": "Data source",
                    "query": "prometheus",
                    "current": {},
                    "hide": 0,
                },
                {
                    "type": "query",
                    "name": "instance",
                    "label": "Instance",
                    "datasource": DS,
                    "query": {"query": 'label_values(up{job="agencyos-assistant"}, instance)', "refId": "instance"},
                    "definition": 'label_values(up{job="agencyos-assistant"}, instance)',
                    "includeAll": True,
                    "multi": True,
                    "allValue": ".*",
                    "current": {"text": "All", "value": "$__all"},
                    "refresh": 2,
                    "sort": 1,
                    "hide": 0,
                },
            ]
        },
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                }
            ]
        },
        "links": [
            {
                "title": "Runbooks (LAUNCH_READINESS.md §4)",
                "type": "link",
                "icon": "doc",
                "url": "https://github.com/cennetaitech-netizen/AgencyOS_SLM/blob/HEAD/docs/LAUNCH_READINESS.md",
                "targetBlank": True,
            }
        ],
        "panels": g.panels,
    }


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT}")
