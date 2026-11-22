from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from chat_service.config import Settings
from chat_service.db import create_engine, metadata

TEST_DB = os.environ.get("CHAT_TEST_DATABASE_URL", "postgresql+asyncpg://chat:chat@localhost:5432/chat_test")


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        from datetime import timedelta

        self.now = self.now + timedelta(**kwargs)


@pytest.fixture(scope="session")
async def engine():
    eng = create_engine(TEST_DB)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def clean_db(engine):
    yield
    names = ", ".join(t.name for t in reversed(metadata.sorted_tables))
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def settings() -> Settings:
    return Settings(env="test", database_url=TEST_DB, billing_mode="live")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
