"""Scheduled maintenance jobs. Run every 5 minutes: ``python -m chat_service.jobs`` (or ``--once``).

* release abandoned reservations
* expire credit buckets
* retry pending refund/chargeback reversals
* retention: delete conversations older than CHAT_RETENTION_DAYS (daily is enough; cheap to run often)
* wallet invariant check: alert on any mismatch
* chat actions: expire stale proposals and approval requests
* subscriptions: close billing cycles that ended (invoice + renew), dunning (past due → suspended)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from chat_service.actions.service import ActionService
from chat_service.billing.invoices import InvoiceService
from chat_service.billing.subscriptions import SubscriptionService
from chat_service.billing.wallet import WalletService
from chat_service.config import Settings, get_settings
from chat_service.db import create_engine
from chat_service.db.schema import chat_wallets
from chat_service.engine.store import ConversationStore, retention_cutoff
from chat_service.llm.router import RolloutStore
from chat_service.tools import default_product_api

log = logging.getLogger("chat_service.jobs")


async def run_once(
    wallet: WalletService,
    conversations: ConversationStore,
    settings: Settings,
    actions: ActionService | None = None,
    subscriptions: SubscriptionService | None = None,
    rollout: RolloutStore | None = None,
) -> dict[str, int]:
    result = {
        "reservations_expired": await wallet.expire_reservations(),
        "buckets_expired": await wallet.expire_buckets(),
        "reversals_completed": await wallet.process_pending_reversals(),
        "conversations_deleted": await conversations.delete_older_than(
            retention_cutoff(datetime.now(UTC), settings.retention_days)
        ),
    }
    async with wallet.engine.connect() as conn:
        org_ids = (await conn.execute(select(chat_wallets.c.org_id))).scalars().all()
    broken = [org for org in org_ids if not await wallet.check_invariant(org)]
    for org in broken:
        log.error("wallet invariant broken for org %s", org)  # wire to alerting (PagerDuty/Slack) in Phase 7
    result["invariant_failures"] = len(broken)
    if actions is not None:
        result["actions_expired"] = await actions.expire()
    if subscriptions is not None:
        result["invoices_created"] = len(await subscriptions.close_due_cycles())
        result["subscriptions_suspended"] = await subscriptions.run_dunning()
    if rollout is not None:
        decision = await rollout.evaluate_canary(settings)
        result["canary_rolled_back"] = int(decision.get("decision") == "rolled_back")
    return result


async def main(once: bool, interval_seconds: int) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    wallet = WalletService(engine, settings)
    conversations = ConversationStore(engine)
    actions = ActionService(engine, settings, default_product_api(settings))
    subscriptions = SubscriptionService(engine, settings, wallet, InvoiceService(engine, settings))
    rollout = RolloutStore(engine, settings.model_name)
    try:
        while True:
            log.info("jobs: %s", await run_once(wallet, conversations, settings, actions, subscriptions, rollout))
            if once:
                return
            await asyncio.sleep(interval_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    asyncio.run(main(args.once, args.interval))
