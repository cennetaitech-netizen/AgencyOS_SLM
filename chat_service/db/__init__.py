"""Database engine helpers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from chat_service.db.schema import metadata

__all__ = ["create_engine", "metadata"]


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
