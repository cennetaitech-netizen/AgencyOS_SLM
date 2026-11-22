"""Database schema: the shared contract for all workstreams.

Covers Milestone M1 (wallet, chat, feedback) and the tables M2 needs (actions, plans, caps, invoices),
so every workstream builds against the same schema from day one.
See docs/IMPLEMENTATION_GUIDE.md Section 6.1, Phase 9 and Phase 10.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Sequence,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def _ts(name: str, nullable: bool = False, default_now: bool = True) -> Column:
    return Column(
        name,
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


# --------------------------------------------------------------------------- wallet & metering

chat_wallets = Table(
    "chat_wallets",
    metadata,
    Column("org_id", UUID(as_uuid=True), primary_key=True),
    Column("tokens_available", BigInteger, nullable=False, server_default="0"),
    Column("tokens_reserved", BigInteger, nullable=False, server_default="0"),
    _ts("updated_at"),
    CheckConstraint("tokens_available >= 0", name="available_non_negative"),
    CheckConstraint("tokens_reserved >= 0", name="reserved_non_negative"),
    CheckConstraint("tokens_reserved <= tokens_available", name="reserved_within_available"),
)

chat_credit_buckets = Table(
    "chat_credit_buckets",
    metadata,
    Column("bucket_id", UUID(as_uuid=True), primary_key=True),
    Column("org_id", UUID(as_uuid=True), nullable=False, index=True),
    Column("source", String(32), nullable=False),
    Column("tokens_granted", BigInteger, nullable=False),
    Column("tokens_used", BigInteger, nullable=False, server_default="0"),
    _ts("granted_at"),
    _ts("expires_at", nullable=True, default_now=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("payment_id", UUID(as_uuid=True), nullable=True),
    Column("granted_by", UUID(as_uuid=True), nullable=True),
    Column("note", Text, nullable=True),
    CheckConstraint("source IN ('free_trial','plan_monthly','pack','goodwill')", name="source_valid"),
    CheckConstraint("status IN ('active','exhausted','expired','reversed')", name="status_valid"),
    CheckConstraint("tokens_granted > 0", name="granted_positive"),
    CheckConstraint("tokens_used >= 0 AND tokens_used <= tokens_granted", name="used_within_granted"),
)
Index(
    "uq_chat_one_free_trial_per_org",
    chat_credit_buckets.c.org_id,
    unique=True,
    postgresql_where=text("source = 'free_trial'"),
)

chat_reservations = Table(
    "chat_reservations",
    metadata,
    Column("reservation_id", UUID(as_uuid=True), primary_key=True),
    Column("org_id", UUID(as_uuid=True), nullable=False, index=True),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("conversation_id", UUID(as_uuid=True), nullable=False),
    Column("message_id", UUID(as_uuid=True), nullable=False, unique=True),
    Column("tokens_reserved", BigInteger, nullable=False),
    Column("overage_reserved", BigInteger, nullable=False, server_default="0"),  # part beyond the buckets
    Column("status", String(16), nullable=False),
    _ts("created_at"),
    _ts("expires_at", default_now=False),
    CheckConstraint("tokens_reserved > 0", name="reserved_positive"),
    CheckConstraint("overage_reserved >= 0 AND overage_reserved <= tokens_reserved", name="overage_within"),
    CheckConstraint("status IN ('held','settled','released','expired')", name="status_valid"),
)

chat_usage_events = Table(
    "chat_usage_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _ts("occurred_at"),
    Column("org_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("workspace_id", UUID(as_uuid=True), nullable=True),
    Column("client_id", String(128), nullable=True),
    Column("conversation_id", UUID(as_uuid=True), nullable=False),
    Column("message_id", UUID(as_uuid=True), nullable=False, unique=True),
    Column("billable_input_tokens", Integer, nullable=False),
    Column("billable_output_tokens", Integer, nullable=False),
    Column("tokens_charged", Integer, nullable=False),
    Column("overage_tokens", Integer, nullable=False, server_default="0"),  # part of tokens_charged billed as overage
    Column("billing_tokenizer_version", String(64), nullable=False),
    Column("host_prompt_tokens", Integer, nullable=True),
    Column("host_completion_tokens", Integer, nullable=True),
    Column("model_name", String(128), nullable=False),
    Column("serving_host", String(16), nullable=False, server_default="primary"),
    Column("prompt_version", String(64), nullable=False),
    Column("steps", Integer, nullable=False, server_default="0"),
    Column("status", String(16), nullable=False),
    Column("reason", String(64), nullable=True),
    CheckConstraint("status IN ('charged','not_charged','shadow')", name="status_valid"),
)
Index("ix_chat_usage_events_org_time", chat_usage_events.c.org_id, chat_usage_events.c.occurred_at)

chat_bucket_debits = Table(
    "chat_bucket_debits",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("message_id", UUID(as_uuid=True), nullable=False, index=True),
    Column("bucket_id", UUID(as_uuid=True), ForeignKey("chat_credit_buckets.bucket_id"), nullable=False),
    Column("tokens", BigInteger, nullable=False),
    CheckConstraint("tokens > 0", name="tokens_positive"),
)

# --------------------------------------------------------------------------- conversations

chat_conversations = Table(
    "chat_conversations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("org_id", UUID(as_uuid=True), nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("title", Text, nullable=True),
    _ts("created_at"),
    _ts("updated_at"),
)
Index("ix_chat_conversations_user", chat_conversations.c.org_id, chat_conversations.c.user_id)

chat_messages = Table(
    "chat_messages",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey("chat_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False),
    Column("tokens_charged", Integer, nullable=True),
    Column("prompt_version", String(64), nullable=True),
    Column("model_name", String(128), nullable=True),
    Column("flags", JSONB, nullable=True),  # quality flags, e.g. {"ungrounded_numbers": [...]}
    _ts("created_at"),
    CheckConstraint("role IN ('user','assistant')", name="role_valid"),
)

chat_tool_calls = Table(
    "chat_tool_calls",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "message_id",
        UUID(as_uuid=True),
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("step", Integer, nullable=False),
    Column("tool_name", String(128), nullable=False),
    Column("args", JSONB, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("result", JSONB, nullable=True),  # encrypt at rest at the storage layer; retention-limited
    Column("result_chars", Integer, nullable=True),
    Column("truncated", Boolean, nullable=True),
    Column("duration_ms", Integer, nullable=True),
    Column("error", Text, nullable=True),
    _ts("created_at"),
)

chat_feedback = Table(
    "chat_feedback",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column(
        "message_id",
        UUID(as_uuid=True),
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column("rating", String(8), nullable=False),
    Column("reason", String(64), nullable=True),
    Column("comment", Text, nullable=True),
    _ts("created_at"),
    CheckConstraint("rating IN ('up','down')", name="rating_valid"),
    UniqueConstraint("message_id", "user_id", name="uq_chat_feedback_message_user"),
)

# --------------------------------------------------------------------------- org settings & billing profile

chat_org_settings = Table(
    "chat_org_settings",
    metadata,
    Column("org_id", UUID(as_uuid=True), primary_key=True),
    Column("chat_enabled", Boolean, nullable=False, server_default="false"),
    Column("billing_mode", String(8), nullable=True),  # NULL = use global default
    Column("training_consent", Boolean, nullable=False, server_default="false"),
    _ts("training_consent_at", nullable=True, default_now=False),
    Column("training_consent_by", UUID(as_uuid=True), nullable=True),
    Column("timezone", String(64), nullable=False, server_default="UTC"),
    Column("currency", String(3), nullable=False, server_default="USD"),
    Column("model_pin", String(128), nullable=True),  # pin this org to a model version (rollout safety)
    _ts("updated_at"),
    CheckConstraint("billing_mode IS NULL OR billing_mode IN ('shadow','live')", name="mode_valid"),
)

chat_billing_profiles = Table(
    "chat_billing_profiles",
    metadata,
    Column("org_id", UUID(as_uuid=True), primary_key=True),
    Column("legal_name", Text, nullable=False),
    Column("address", JSONB, nullable=False),
    Column("tax_id", String(64), nullable=True),
    Column("country", String(2), nullable=False),
    Column("billing_email", Text, nullable=False),
    _ts("updated_at"),
)

# --------------------------------------------------------------------------- packs & payments

chat_token_packs = Table(
    "chat_token_packs",
    metadata,
    Column("pack_id", String(64), primary_key=True),
    Column("tokens", BigInteger, nullable=False),
    Column("price_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("validity_days", Integer, nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
)

chat_payments = Table(
    "chat_payments",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("org_id", UUID(as_uuid=True), nullable=False, index=True),
    Column("provider", String(32), nullable=False),
    Column("provider_payment_id", String(255), nullable=False, unique=True),
    Column("pack_id", String(64), ForeignKey("chat_token_packs.pack_id"), nullable=False),
    Column("amount_minor", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_by", UUID(as_uuid=True), nullable=True),
    _ts("created_at"),
    CheckConstraint("status IN ('pending','paid','refunded','disputed','failed')", name="status_valid"),
)

# --------------------------------------------------------------------------- actions (Phase 9)

chat_actions = Table(
    "chat_actions",
    metadata,
    Column("action_id", UUID(as_uuid=True), primary_key=True),
    Column("org_id", UUID(as_uuid=True), nullable=False, index=True),
    Column("conversation_id", UUID(as_uuid=True), nullable=False),
    Column("message_id", UUID(as_uuid=True), nullable=False),
    Column("requested_by", UUID(as_uuid=True), nullable=False),
    Column("tool_name", String(128), nullable=False),
    Column("risk_tier", Integer, nullable=False),
    Column("args", JSONB, nullable=False),
    Column("before_state", JSONB, nullable=True),
    Column("proposed_after", JSONB, nullable=True),
    Column("rule_warnings", JSONB, nullable=True),
    Column("status", String(32), nullable=False),
    Column("confirmed_by", UUID(as_uuid=True), nullable=True),
    Column("second_approved_by", UUID(as_uuid=True), nullable=True),
    _ts("executed_at", nullable=True, default_now=False),
    Column("platform_response", JSONB, nullable=True),
    Column("undo_of", UUID(as_uuid=True), nullable=True),
    Column("target_id", String(128), nullable=True),
    Column("spend_change_minor", BigInteger, nullable=False, server_default="0"),
    Column("requires_second_approval", Boolean, nullable=False, server_default="false"),
    Column("error", Text, nullable=True),
    _ts("expires_at", default_now=False),
    _ts("created_at"),
    CheckConstraint("risk_tier IN (1,2,3)", name="tier_valid"),
    CheckConstraint(
        "status IN ('proposed','awaiting_second_approval','confirmed','executing','executed',"
        "'failed','cancelled','expired','rejected','undone')",
        name="status_valid",
    ),
)

chat_action_rules = Table(
    "chat_action_rules",
    metadata,
    Column("org_id", UUID(as_uuid=True), primary_key=True),
    Column("write_actions_enabled", Boolean, nullable=False, server_default="false"),
    Column("max_budget_change_pct", Numeric, nullable=False, server_default="20"),
    Column("max_budget_change_abs_minor", BigInteger, nullable=True),
    Column("second_approval_above_minor", BigInteger, nullable=True),
    Column("daily_spend_change_cap_minor", BigInteger, nullable=True),
    Column("tier1_requires_confirmation", Boolean, nullable=False, server_default="true"),
    Column("roles_by_tier", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("blocked_tools", ARRAY(Text), nullable=False, server_default=text("'{}'")),
)

# --------------------------------------------------------------------------- plans, caps, invoices (Phase 10)

chat_plans = Table(
    "chat_plans",
    metadata,
    Column("plan_id", String(64), primary_key=True),
    Column("name", Text, nullable=False),
    Column("monthly_fee_minor", BigInteger, nullable=False, server_default="0"),
    Column("currency", String(3), nullable=False),
    Column("monthly_included_tokens", BigInteger, nullable=False, server_default="0"),
    Column("overage_allowed", Boolean, nullable=False, server_default="false"),
    Column("price_per_1m_tokens_minor", BigInteger, nullable=True),
    Column("max_output_tokens_per_msg", Integer, nullable=False, server_default="1200"),
    Column("requests_per_minute", Integer, nullable=False, server_default="20"),
    Column("active", Boolean, nullable=False, server_default="true"),
)

chat_subscriptions = Table(
    "chat_subscriptions",
    metadata,
    Column("org_id", UUID(as_uuid=True), primary_key=True),
    Column("plan_id", String(64), ForeignKey("chat_plans.plan_id"), nullable=False),
    _ts("current_period_start", default_now=False),
    _ts("current_period_end", default_now=False),
    Column("spend_limit_minor", BigInteger, nullable=True),  # overage hard stop; NULL = no overage
    Column("status", String(16), nullable=False),
    Column("pending_plan_id", String(64), ForeignKey("chat_plans.plan_id"), nullable=True),  # downgrade at renewal
    _ts("past_due_since", nullable=True, default_now=False),
    Column("provider_subscription_id", String(255), nullable=True),
    _ts("updated_at"),
    CheckConstraint("status IN ('active','past_due','suspended','cancelled')", name="status_valid"),
)

chat_usage_caps = Table(
    "chat_usage_caps",
    metadata,
    Column("cap_id", UUID(as_uuid=True), primary_key=True),
    Column("org_id", UUID(as_uuid=True), nullable=False, index=True),
    Column("scope_type", String(16), nullable=False),
    Column("scope_id", UUID(as_uuid=True), nullable=False),
    Column("period", String(8), nullable=False),
    Column("max_tokens", BigInteger, nullable=False),
    Column("hard", Boolean, nullable=False, server_default="true"),
    CheckConstraint("scope_type IN ('org','workspace','user')", name="scope_valid"),
    CheckConstraint("period IN ('day','month')", name="period_valid"),
)

chat_invoices = Table(
    "chat_invoices",
    metadata,
    Column("invoice_id", UUID(as_uuid=True), primary_key=True),
    Column("org_id", UUID(as_uuid=True), nullable=False, index=True),
    Column("invoice_number", String(64), nullable=True, unique=True),
    _ts("period_start", default_now=False),
    _ts("period_end", default_now=False),
    Column("currency", String(3), nullable=False),
    Column("subtotal_minor", BigInteger, nullable=False),
    Column("tax_minor", BigInteger, nullable=False, server_default="0"),
    Column("total_minor", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("kind", String(32), nullable=False, server_default="cycle"),
    Column("overage_tokens", BigInteger, nullable=False, server_default="0"),
    _ts("reconciled_at", nullable=True, default_now=False),
    Column("approved_by", UUID(as_uuid=True), nullable=True),
    Column("provider_invoice_id", String(255), nullable=True),
    _ts("created_at"),
    CheckConstraint("status IN ('draft','approved','sent','paid','void')", name="status_valid"),
    CheckConstraint("kind IN ('cycle','subscription_start','proration')", name="kind_valid"),
)

chat_invoice_lines = Table(
    "chat_invoice_lines",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "invoice_id",
        UUID(as_uuid=True),
        ForeignKey("chat_invoices.invoice_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("kind", String(32), nullable=False),  # plan_fee, overage, credit
    Column("description", Text, nullable=False),
    Column("quantity", BigInteger, nullable=False),
    Column("unit_price_minor", BigInteger, nullable=True),
    Column("amount_minor", BigInteger, nullable=False),
    Column("breakdown", JSONB, nullable=True),
)

invoice_number_seq = Sequence("chat_invoice_number_seq", metadata=metadata)

chat_usage_alerts = Table(
    "chat_usage_alerts",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("org_id", UUID(as_uuid=True), nullable=False),
    Column("scope", String(64), nullable=False),  # e.g. plan_allowance, cap:<cap_id>, spend_limit
    Column("period_key", String(32), nullable=False),  # e.g. 2026-11 or 2026-11-22
    Column("threshold", Integer, nullable=False),  # 50 / 80 / 90 / 100
    _ts("created_at"),
    UniqueConstraint("org_id", "scope", "period_key", "threshold", name="uq_chat_usage_alerts_once"),
)

# --------------------------------------------------------------------------- model rollout (Phase 11)

chat_model_rollout = Table(
    "chat_model_rollout",
    metadata,
    Column("id", String(32), primary_key=True),  # single row: 'default'
    Column("production_model", String(128), nullable=False),
    Column("canary_model", String(128), nullable=True),
    Column("canary_percent", Integer, nullable=False, server_default="0"),
    Column("status", String(16), nullable=False, server_default="idle"),
    Column("note", Text, nullable=True),
    _ts("updated_at"),
    CheckConstraint("canary_percent >= 0 AND canary_percent <= 100", name="percent_valid"),
    CheckConstraint("status IN ('idle','canary','promoted','rolled_back')", name="status_valid"),
)
