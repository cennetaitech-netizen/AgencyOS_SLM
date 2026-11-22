"""Money checks after a load test: nothing leaked, nothing double-charged.

  python ops/verify_after_load.py --database-url postgresql+asyncpg://... --summary load-summary.json

* every wallet: tokens_available == sum of active bucket balances (invariant)
* no reservations still held
* every org: tokens taken from buckets == tokens charged in the usage ledger (no overage in this test)
* charged usage events == successful messages reported by the load test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from sqlalchemy import func, select

from chat_service.billing.wallet import WalletService
from chat_service.config import Settings
from chat_service.db import create_engine
from chat_service.db.schema import chat_bucket_debits, chat_reservations, chat_usage_events


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-url", required=True)
    ap.add_argument("--summary", required=True)
    args = ap.parse_args()
    with open(args.summary) as f:  # noqa: ASYNC230 - small file before any async work
        summary = json.load(f)
    orgs = [uuid.UUID(o) for o in summary["orgs_list"]]
    engine = create_engine(args.database_url)
    wallet = WalletService(engine, Settings(env="test", database_url=args.database_url))
    problems = []
    try:
        for org in orgs:
            if not await wallet.check_invariant(org):
                problems.append(f"invariant broken for {org}")
        async with engine.connect() as conn:
            held = (
                await conn.execute(
                    select(func.count()).where(
                        chat_reservations.c.org_id.in_(orgs), chat_reservations.c.status == "held"
                    )
                )
            ).scalar_one()
            charged_events, charged_tokens = (
                await conn.execute(
                    select(func.count(), func.coalesce(func.sum(chat_usage_events.c.tokens_charged), 0)).where(
                        chat_usage_events.c.org_id.in_(orgs), chat_usage_events.c.status == "charged"
                    )
                )
            ).one()
            debited = (
                await conn.execute(
                    select(func.coalesce(func.sum(chat_bucket_debits.c.tokens), 0))
                    .join(chat_usage_events, chat_usage_events.c.message_id == chat_bucket_debits.c.message_id)
                    .where(chat_usage_events.c.org_id.in_(orgs))
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    if held:
        problems.append(f"{held} reservations still held")
    if int(debited) != int(charged_tokens):
        problems.append(f"bucket debits {debited} != charged tokens {charged_tokens}")
    if charged_events != summary["outcomes"].get("ok", 0):
        problems.append(f"charged events {charged_events} != successful messages {summary['outcomes'].get('ok', 0)}")
    print(
        json.dumps(
            {
                "orgs_checked": len(orgs),
                "charged_messages": charged_events,
                "charged_tokens": int(charged_tokens),
                "bucket_debits": int(debited),
                "reservations_held": held,
                "problems": problems,
            },
            indent=2,
        )
    )
    raise SystemExit(1 if problems else 0)


if __name__ == "__main__":
    asyncio.run(main())
