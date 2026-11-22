"""Invoices for token consumption (Phase 10).

* Lines: plan fee (in advance), overage (in arrears), proration, credits.
* Reconciliation: invoice lines must equal the subtotal, and invoiced overage must equal the usage ledger.
  Unreconciled invoices can't be approved.
* Numbering: sequential numbers are assigned at approval, so voided drafts leave no gaps.
* Tax: one rate per buyer country from ``CHAT_TAX_RATES`` (finance input). Jurisdiction-specific
  splits (e.g. CGST/SGST vs IGST) and e-invoicing are handled by the invoicing route chosen in D9.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from chat_service.config import Settings
from chat_service.db.schema import (
    chat_billing_profiles,
    chat_invoice_lines,
    chat_invoices,
    chat_usage_events,
    invoice_number_seq,
)


class InvoiceError(Exception):
    pass


@dataclass
class Line:
    kind: str  # plan_fee | overage | proration | credit
    description: str
    quantity: int
    amount_minor: int
    unit_price_minor: int | None = None
    breakdown: dict[str, Any] | None = None


@dataclass
class InvoiceView:
    invoice_id: uuid.UUID
    org_id: uuid.UUID
    invoice_number: str | None
    kind: str
    status: str
    currency: str
    period_start: datetime
    period_end: datetime
    subtotal_minor: int
    tax_minor: int
    total_minor: int
    overage_tokens: int
    reconciled: bool
    lines: list[dict[str, Any]] = field(default_factory=list)


class InvoiceSink(Protocol):
    """Where approved invoices go: payment-provider invoicing or the accounting system (decision D9)."""

    async def send(self, invoice: InvoiceView) -> str: ...


class LogInvoiceSink:
    def __init__(self) -> None:
        self.sent: list[InvoiceView] = []

    async def send(self, invoice: InvoiceView) -> str:
        self.sent.append(invoice)
        return f"dev_inv_{invoice.invoice_id.hex[:12]}"


def overage_amount_minor(tokens: int, price_per_1m_minor: int) -> int:
    return int((Decimal(tokens) * Decimal(price_per_1m_minor) / Decimal(1_000_000)).quantize(0, ROUND_HALF_UP))


class InvoiceService:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        sink: InvoiceSink | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.sink = sink or LogInvoiceSink()
        self.clock = clock

    # ------------------------------------------------------------------ create

    async def create_in(
        self,
        conn: AsyncConnection,
        org_id: uuid.UUID,
        kind: str,
        period_start: datetime,
        period_end: datetime,
        currency: str,
        lines: list[Line],
        overage_tokens: int = 0,
    ) -> uuid.UUID | None:
        """Create an invoice inside the caller's transaction. Returns None if there's nothing to bill."""
        lines = [ln for ln in lines if ln.amount_minor != 0]
        if not lines:
            return None
        subtotal = sum(ln.amount_minor for ln in lines)
        profile = (
            await conn.execute(select(chat_billing_profiles).where(chat_billing_profiles.c.org_id == org_id))
        ).first()
        rate = Decimal(str(self.settings.tax_rates.get(profile.country, 0))) if profile else Decimal(0)
        tax = int((Decimal(subtotal) * rate / 100).quantize(0, ROUND_HALF_UP)) if subtotal > 0 else 0
        invoice_id = uuid.uuid4()
        await conn.execute(
            chat_invoices.insert().values(
                invoice_id=invoice_id,
                org_id=org_id,
                kind=kind,
                period_start=period_start,
                period_end=period_end,
                currency=currency,
                subtotal_minor=subtotal,
                tax_minor=tax,
                total_minor=subtotal + tax,
                overage_tokens=overage_tokens,
                status="draft",
                created_at=self.clock(),
            )
        )
        await conn.execute(
            chat_invoice_lines.insert(),
            [
                {
                    "invoice_id": invoice_id,
                    "kind": ln.kind,
                    "description": ln.description,
                    "quantity": ln.quantity,
                    "unit_price_minor": ln.unit_price_minor,
                    "amount_minor": ln.amount_minor,
                    "breakdown": ln.breakdown,
                }
                for ln in lines
            ],
        )
        return invoice_id

    # ------------------------------------------------------------------ reconcile / approve / send

    async def reconcile(self, invoice_id: uuid.UUID) -> bool:
        """Lines == subtotal, total == subtotal + tax, and overage == the usage ledger for the period."""
        async with self.engine.begin() as conn:
            inv = (
                await conn.execute(
                    select(chat_invoices).where(chat_invoices.c.invoice_id == invoice_id).with_for_update()
                )
            ).one()
            lines_total = (
                await conn.execute(
                    select(func.coalesce(func.sum(chat_invoice_lines.c.amount_minor), 0)).where(
                        chat_invoice_lines.c.invoice_id == invoice_id
                    )
                )
            ).scalar_one()
            invoiced_overage = (
                await conn.execute(
                    select(func.coalesce(func.sum(chat_invoice_lines.c.quantity), 0)).where(
                        chat_invoice_lines.c.invoice_id == invoice_id, chat_invoice_lines.c.kind == "overage"
                    )
                )
            ).scalar_one()
            ok = int(lines_total) == inv.subtotal_minor and inv.total_minor == inv.subtotal_minor + inv.tax_minor
            ok = ok and int(invoiced_overage) == inv.overage_tokens
            if inv.kind == "cycle":
                ledger = (
                    await conn.execute(
                        select(func.coalesce(func.sum(chat_usage_events.c.overage_tokens), 0)).where(
                            chat_usage_events.c.org_id == inv.org_id,
                            chat_usage_events.c.occurred_at >= inv.period_start,
                            chat_usage_events.c.occurred_at < inv.period_end,
                        )
                    )
                ).scalar_one()
                ok = ok and int(ledger) == inv.overage_tokens
            await conn.execute(
                update(chat_invoices)
                .where(chat_invoices.c.invoice_id == invoice_id)
                .values(reconciled_at=self.clock() if ok else None)
            )
            return ok

    async def approve_and_send(self, invoice_id: uuid.UUID, approver: uuid.UUID | None) -> InvoiceView:
        """Finance approval: requires reconciliation; assigns the sequential number; sends to the sink."""
        if not await self.reconcile(invoice_id):
            raise InvoiceError("invoice doesn't reconcile with the usage ledger; investigate before approving")
        async with self.engine.begin() as conn:
            inv = (
                await conn.execute(
                    select(chat_invoices).where(chat_invoices.c.invoice_id == invoice_id).with_for_update()
                )
            ).one()
            if inv.status != "draft":
                raise InvoiceError(f"invoice is {inv.status}")
            seq = (await conn.execute(select(invoice_number_seq.next_value()))).scalar_one()
            number = f"{self.settings.invoice_number_prefix}-{self.clock().year}-{seq:06d}"
            await conn.execute(
                update(chat_invoices)
                .where(chat_invoices.c.invoice_id == invoice_id)
                .values(status="approved", invoice_number=number, approved_by=approver)
            )
        view = await self.get(inv.org_id, invoice_id)
        provider_id = await self.sink.send(view)
        async with self.engine.begin() as conn:
            await conn.execute(
                update(chat_invoices)
                .where(chat_invoices.c.invoice_id == invoice_id)
                .values(status="sent", provider_invoice_id=provider_id)
            )
        return await self.get(inv.org_id, invoice_id)

    async def auto_send_if_allowed(self, invoice_id: uuid.UUID | None) -> None:
        if invoice_id is not None and not self.settings.invoice_require_approval:
            await self.approve_and_send(invoice_id, None)

    async def mark_paid(self, provider_invoice_id: str) -> uuid.UUID:
        async with self.engine.begin() as conn:
            row = (
                await conn.execute(
                    update(chat_invoices)
                    .where(chat_invoices.c.provider_invoice_id == provider_invoice_id)
                    .values(status="paid")
                    .returning(chat_invoices.c.org_id)
                )
            ).first()
        if row is None:
            raise InvoiceError("unknown invoice")
        return row.org_id

    async def void(self, invoice_id: uuid.UUID) -> None:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(chat_invoices)
                .where(chat_invoices.c.invoice_id == invoice_id, chat_invoices.c.status == "draft")
                .values(status="void")
            )
        if result.rowcount == 0:
            raise InvoiceError("only draft invoices can be voided; issue a credit note instead")

    # ------------------------------------------------------------------ read

    async def get(self, org_id: uuid.UUID, invoice_id: uuid.UUID) -> InvoiceView:
        async with self.engine.connect() as conn:
            inv = (
                await conn.execute(
                    select(chat_invoices).where(
                        chat_invoices.c.invoice_id == invoice_id, chat_invoices.c.org_id == org_id
                    )
                )
            ).first()
            if inv is None:
                raise InvoiceError("invoice not found")
            lines = (
                await conn.execute(
                    select(chat_invoice_lines)
                    .where(chat_invoice_lines.c.invoice_id == invoice_id)
                    .order_by(chat_invoice_lines.c.id)
                )
            ).all()
        return InvoiceView(
            inv.invoice_id,
            inv.org_id,
            inv.invoice_number,
            inv.kind,
            inv.status,
            inv.currency,
            inv.period_start,
            inv.period_end,
            inv.subtotal_minor,
            inv.tax_minor,
            inv.total_minor,
            inv.overage_tokens,
            inv.reconciled_at is not None,
            [
                {
                    "kind": ln.kind,
                    "description": ln.description,
                    "quantity": ln.quantity,
                    "unit_price_minor": ln.unit_price_minor,
                    "amount_minor": ln.amount_minor,
                    "breakdown": ln.breakdown,
                }
                for ln in lines
            ],
        )

    async def list(self, org_id: uuid.UUID, include_drafts: bool = False) -> list[dict[str, Any]]:
        q = select(chat_invoices).where(chat_invoices.c.org_id == org_id)
        if not include_drafts:
            q = q.where(chat_invoices.c.status.in_(("approved", "sent", "paid")))
        async with self.engine.connect() as conn:
            rows = (await conn.execute(q.order_by(chat_invoices.c.created_at.desc()))).all()
        return [
            {
                "invoice_id": str(r.invoice_id),
                "invoice_number": r.invoice_number,
                "kind": r.kind,
                "status": r.status,
                "currency": r.currency,
                "period_start": r.period_start,
                "period_end": r.period_end,
                "total_minor": r.total_minor,
            }
            for r in rows
        ]

    async def usage_csv(self, org_id: uuid.UUID, invoice_id: uuid.UUID) -> str:
        """Message-level usage behind an invoice, for the client's own audit and re-billing."""
        inv = await self.get(org_id, invoice_id)
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(chat_usage_events)
                    .where(
                        chat_usage_events.c.org_id == org_id,
                        chat_usage_events.c.occurred_at >= inv.period_start,
                        chat_usage_events.c.occurred_at < inv.period_end,
                    )
                    .order_by(chat_usage_events.c.occurred_at, chat_usage_events.c.id)
                )
            ).all()
        buf = io.StringIO()
        cols = [
            "occurred_at",
            "user_id",
            "workspace_id",
            "client_id",
            "conversation_id",
            "message_id",
            "billable_input_tokens",
            "billable_output_tokens",
            "tokens_charged",
            "overage_tokens",
            "status",
            "reason",
        ]
        writer = csv.writer(buf)
        writer.writerow(cols)
        for r in rows:
            writer.writerow([getattr(r, c) for c in cols])
        return buf.getvalue()
