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
    prompt_version: str = "system_v1"

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

    # Roles (names as the product issues them; confirm in Phase 0)
    org_admin_role: str = "org_admin"
    staff_role: str = "agencyos_staff"


@lru_cache
def get_settings() -> Settings:
    return Settings()
