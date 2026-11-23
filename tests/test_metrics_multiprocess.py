"""With several worker processes, /metrics must report the sum across workers, not whichever worker answered."""

from __future__ import annotations

import os
import subprocess
import sys

COUNT = (
    "from chat_service import metrics\n"
    "metrics.MESSAGES.labels('ok', 'live').inc({n})\n"
    "metrics.HOST_CACHED_TOKENS.labels('m').inc(100)\n"
    "metrics.TTFT.observe(0.4)\n"
)
RENDER = "from chat_service import metrics\nprint(metrics.render()[0].decode())\n"


def run(code: str, env: dict) -> str:
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout


def test_metrics_are_summed_across_worker_processes(tmp_path):
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    for n in (2, 3):  # two "workers", each handling some messages
        run(COUNT.format(n=n), env)
    text = run(RENDER, env)  # a third process answers the scrape
    assert 'aos_chat_messages_total{billing_mode="live",status="ok"} 5.0' in text
    assert 'aos_chat_host_cached_prompt_tokens_total{model="m"} 200.0' in text
    assert "aos_chat_time_to_first_token_seconds_count 2.0" in text


def test_single_process_mode_is_unchanged():
    env = {k: v for k, v in os.environ.items() if k != "PROMETHEUS_MULTIPROC_DIR"}
    text = run(COUNT.format(n=1) + RENDER.split("\n", 1)[1], env)
    assert 'aos_chat_messages_total{billing_mode="live",status="ok"} 1.0' in text
