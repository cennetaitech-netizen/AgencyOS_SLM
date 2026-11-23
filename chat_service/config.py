"""Service configuration. Every value can be set by an environment variable prefixed with ``CHAT_``.

Defaults follow docs/IMPLEMENTATION_GUIDE.md Section 3.1.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHAT_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "staging", "production"] = "dev"
    database_url: str = "postgresql+asyncpg://chat:chat@localhost:5432/chat_dev"

    # Model host (OpenAI-compatible)
    model_base_url: str = "http://localhost:8001/v1"
    model_api_key: str = Field(default="", repr=False)
    model_name: str = "media-slm-dev"
    extra_model_names: list[str] = Field(default_factory=list)  # e.g. fine-tuned LoRA versions on the same host
    model_fallback_base_url: str | None = None
    model_fallback_api_key: str = Field(default="", repr=False)
    model_fallback_name: str | None = None
    model_timeout_seconds: float = 60.0
    model_temperature: float = 0.2

    # Agent loop limits
    max_steps: int = 5
    max_output_tokens: int = 1200
    min_useful_output: int = 200
    max_message_chars: int = 8000
    history_turns: int = 10
    max_turns_per_conversation: int = 40
    tool_timeout_seconds: float = 15.0
    tool_result_max_chars: int = 8000
    prompt_version: str = "system_v3"  # static, cache-friendly (see engine/prompting.py); v1 still works
    # where the [Context] line goes: first line of the latest user message (default; best on Qwen2.5-3B, works with
    # every chat template) or its own system message before it (see docs/chat/PROMPT_V3_LOCAL_EVAL.md)
    prompt_context_placement: Literal["user_prefix", "system_message"] = "user_prefix"

    # Wallet / billing
    reservation_ttl_seconds: int = 300
    free_trial_tokens: int = 200_000
    free_trial_days: int = 30
    low_balance_tokens: int = 20_000
    bill_refusals: bool = False
    billing_mode: Literal["shadow", "live"] = "shadow"
    billing_tokenizer_version: str = "dev-regex-v0"
    billing_tokenizer_path: str | None = None

    # Cost protection
    enabled_global: bool = True
    write_actions_enabled_global: bool = False
    global_daily_host_token_ceiling: int = 50_000_000
    org_daily_max_tokens: int = 2_000_000
    free_trial_daily_max_tokens: int = 50_000

    # Rate limits
    rate_limit_per_user_per_min: int = 20
    max_concurrent_streams_per_org: int = 3

    # Streaming / retention
    sse_heartbeat_seconds: float = 15.0
    retention_days: int = 365

    # Product API (the existing AgencyOS product)
    product_api_base_url: str = "http://localhost:8000"
    product_adapter: Literal["http", "mock"] = "mock"
    auth_provider: Literal["dev", "product"] = "dev"

    # Payments (token packs)
    payment_provider: Literal["dev"] = "dev"  # stripe / razorpay adapters added once finance decides (D9)
    payment_webhook_secret: str = Field(default="dev-webhook-secret", repr=False)
    app_base_url: str = "http://localhost:3000"

    # Subscriptions & invoicing (Phase 10)
    invoice_require_approval: bool = True  # first real invoice runs need finance approval
    invoice_number_prefix: str = "AOS"
    tax_rates: dict[str, float] = Field(default_factory=dict)  # country → %, set by finance, e.g. {"IN": 18}
    dunning_grace_days: int = 7
    alert_thresholds: tuple[int, ...] = (50, 80, 90, 100)

    # Unit economics & quality (Phase 7) — prices in minor units per 1M tokens (finance input)
    host_price_input_per_1m_minor: int = 0
    host_price_output_per_1m_minor: int = 0
    # Prompt tokens served from the host's prefix cache. Managed hosts discount them; on our own vLLM they skip
    # prefill, so finance can set the GPU-time equivalent. Unset = charged like other input tokens (conservative).
    host_price_cached_input_per_1m_minor: int | None = None
    list_price_per_1m_tokens_minor: int = 0
    max_overhead_ratio: float = 20.0  # compared with the uncached ratio: cached prompt tokens cost little
    min_gross_margin_pct: float = 70.0

    # Model rollout (Phase 11)
    rollout_min_messages: int = 200  # canary messages needed before judging it
    rollout_max_error_rate_increase: float = 0.02  # +2 points vs production → roll back
    rollout_max_downvote_rate_increase: float = 0.05  # +5 points vs production → roll back

    # Chat actions (Phase 9)
    action_expiry_minutes: int = 10
    undo_window_hours: int = 24
    approver_role: str = "org_admin"
    prompt_version_actions: str = "system_v3_actions"

    # Production hardening (Phase 12)
    cors_allowed_origins: list[str] = Field(default_factory=list)  # e.g. ["https://app.agencyos.example"]
    max_request_bytes: int = 256_000
    data_encryption_keys: list[str] = Field(default_factory=list, repr=False)  # Fernet keys; first = current
    redis_url: str | None = None  # shared rate/concurrency limits across instances
    stream_slot_lease_seconds: int = 180  # a crashed instance's slots free up after this
    metrics_token: str | None = Field(default=None, repr=False)  # bearer token for GET /metrics

    # Roles (names as the product issues them; confirm in Phase 0)
    org_admin_role: str = "org_admin"
    staff_role: str = "agencyos_staff"


def production_problems(s: Settings) -> list[str]:
    """Settings that are fine for development but unsafe in production. The app refuses to start with any."""
    problems = []
    if s.auth_provider == "dev":
        problems.append("CHAT_AUTH_PROVIDER=dev (header-based identities) is not allowed")
    if s.product_adapter != "http":
        problems.append("CHAT_PRODUCT_ADAPTER must be 'http' (the mock product is for development)")
    if s.payment_provider == "dev":
        problems.append("CHAT_PAYMENT_PROVIDER=dev: configure the real payment provider (decision D9)")
    if s.payment_webhook_secret in ("", "dev-webhook-secret", "change-me"):
        problems.append("CHAT_PAYMENT_WEBHOOK_SECRET is missing or a default value")
    if not s.billing_tokenizer_path:
        problems.append("CHAT_BILLING_TOKENIZER_PATH must point to the pinned billing tokenizer")
    if not s.data_encryption_keys:
        problems.append("CHAT_DATA_ENCRYPTION_KEYS is required (encryption of stored tool results)")
    if not s.redis_url:
        problems.append("CHAT_REDIS_URL is required (shared rate and concurrency limits)")
    if not s.cors_allowed_origins or "*" in s.cors_allowed_origins:
        problems.append("CHAT_CORS_ALLOWED_ORIGINS must list the AgencyOS web origins (no '*')")
    if not s.metrics_token:
        problems.append("CHAT_METRICS_TOKEN is required to protect /metrics")
    if "chat:chat@localhost" in s.database_url:
        problems.append("CHAT_DATABASE_URL still points at the local development database")
    return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
