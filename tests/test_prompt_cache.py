"""The prompt prefix (system prompt + tool schemas + history) must be identical across orgs, users and days, so the
model host's prefix cache can reuse it. These tests fail if something personal or dated creeps back into it."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from chat_service.config import Settings
from chat_service.engine.prompting import PromptContext, build_messages, is_static, load_prompt
from chat_service.ml.dataset import prompt_prefix
from chat_service.tools import build_registry
from chat_service.tools.product import MockProductAPI
from chat_service.tools.registry import ToolContext


def test_default_prompts_are_static_and_older_ones_still_work():
    s = Settings()
    for version in (s.prompt_version, s.prompt_version_actions, "system_ft_v2"):
        assert is_static(load_prompt(version)), version
    for version in ("system_v1", "system_v2", "system_ft_v1"):
        assert not is_static(load_prompt(version))
        msgs = build_messages(load_prompt(version), PromptContext("2026-11-23", "UTC", "USD", "planner"), [], "hi")
        assert "2026-11-23" in msgs[0]["content"] and msgs[-1]["content"] == "hi"


def test_prefix_is_identical_for_different_orgs_users_and_days():
    template = load_prompt(Settings().prompt_version_actions)
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "answer"}]
    a = build_messages(template, PromptContext("2026-11-23", "Asia/Kolkata", "INR", "planner", "Priya"), history, "q")
    b = build_messages(template, PromptContext("2027-01-05", "Europe/London", "GBP", "org_admin", "Sam"), history, "q")
    assert a[:-1] == b[:-1]  # system + history: byte-identical
    assert a[-1] == {
        "role": "user",
        "content": "[Context] Today: 2026-11-23 (Asia/Kolkata) · Currency: INR · Role: planner\n\nq",
    }
    assert "Priya" not in json.dumps(a)  # names aren't sent at all
    # optional placement: the context as its own system message before the user's message
    c = build_messages(template, PromptContext("2026-11-23", "UTC", "USD", "planner"), history, "q", "system_message")
    assert c[:-2] == a[:-1] and c[-2]["role"] == "system" and c[-1] == {"role": "user", "content": "q"}


def test_tool_schemas_are_stable_and_lean():
    one = build_registry(Settings(), MockProductAPI()).schemas(include_write=True)
    two = build_registry(Settings(), MockProductAPI()).schemas(include_write=True)
    assert json.dumps(one) == json.dumps(two)
    text = json.dumps(one)
    for keyword in ('"pattern"', '"minLength"', '"maxLength"', '"exclusiveMinimum"', '"additionalProperties": false'):
        assert keyword not in text, keyword
    # a schema-valued additionalProperties describes the values, so the model still sees it
    ad_copy = next(t for t in one if t["function"]["name"] == "validate_ad_copy")
    assert ad_copy["function"]["parameters"]["properties"]["fields"]["additionalProperties"]["type"] == "array"


async def test_arguments_are_still_validated_against_the_full_schema():
    registry = build_registry(Settings(), MockProductAPI())
    ctx = ToolContext(org_id=uuid.uuid4(), user_id=uuid.uuid4(), request_id="r1")
    bad_date = {"date_from": "2026/11/01", "date_to": "2026-11-15"}  # pattern isn't sent, but is enforced
    res = await registry.execute(ctx, "get_campaign_performance", bad_date, 5, 4000)
    assert not res.ok and "invalid arguments" in res.error
    extra = {"date_from": "2026-11-01", "date_to": "2026-11-15", "colour": "red"}  # additionalProperties: false
    res = await registry.execute(ctx, "get_campaign_performance", extra, 5, 4000)
    assert not res.ok and "invalid arguments" in res.error


def test_prefix_size_budget():
    """Characters are a stable proxy for tokens here. Measured with a Qwen tokenizer on 2026-11-23: read-only prefix
    1,355 tokens (was 1,634), with actions 1,731 (was 2,156). Raise these only on purpose."""
    s = Settings()
    registry = build_registry(s, MockProductAPI())
    read_only = len(load_prompt(s.prompt_version)) + len(json.dumps(registry.schemas(False)))
    actions = len(load_prompt(s.prompt_version_actions)) + len(json.dumps(registry.schemas(True)))
    assert read_only <= 5_200, read_only
    assert actions <= 6_800, actions
    assert len(load_prompt("system_ft_v2")) <= 0.5 * len(load_prompt(s.prompt_version_actions))


def test_training_examples_use_the_serving_layout():
    template = load_prompt("system_ft_v2")
    created = datetime(2026, 11, 22, 20, 0, tzinfo=UTC)  # already the 23rd in Kolkata
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "answer"}]
    prefix = prompt_prefix(template, created, "Asia/Kolkata", "INR", history, "How is cmp_101 doing?")
    assert prefix[0] == {"role": "system", "content": template}
    assert prefix[1:3] == history
    assert prefix[3] == {
        "role": "user",
        "content": "[Context] Today: 2026-11-23 (Asia/Kolkata) · Currency: INR · Role: member\n\nHow is cmp_101 doing?",
    }
    assert len(prefix) == 4
