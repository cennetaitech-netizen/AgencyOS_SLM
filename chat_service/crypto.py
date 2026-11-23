"""Field-level encryption at rest (Phase 12) for stored tool results, which hold client campaign data.

* Fernet (AES-128-CBC + HMAC-SHA256) through MultiFernet: the first key in ``CHAT_DATA_ENCRYPTION_KEYS``
  encrypts; all keys decrypt. To rotate, prepend a new key, run ``python -m chat_service.crypto rotate``,
  then remove the old key.
* Encrypted values are stored as ``{"_enc": "fernet", "v": "<token>"}``, so plain historical rows keep working.

Generate a key: ``python -m chat_service.crypto genkey``. Keep keys in the secrets manager, never in code.
A random secret of 32+ characters that isn't a Fernet key (e.g. one a hosting platform generated) is also accepted:
the Fernet key is derived from it with SHA-256, so the same secret always gives the same key.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

_TAG = "fernet"


def as_fernet(key: str | bytes) -> Fernet:
    raw = key.encode() if isinstance(key, str) else key
    try:
        return Fernet(raw)
    except ValueError:
        if len(raw) < 32:
            raise ValueError("encryption key must be a Fernet key or a random secret of 32+ characters") from None
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw).digest()))


class FieldCipher:
    def __init__(self, keys: list[str] | None = None) -> None:
        self._f = MultiFernet([as_fernet(k) for k in keys]) if keys else None

    @property
    def enabled(self) -> bool:
        return self._f is not None

    def encrypt_json(self, value: Any) -> Any:
        if self._f is None or value is None:
            return value
        token = self._f.encrypt(json.dumps(value, default=str, separators=(",", ":")).encode()).decode()
        return {"_enc": _TAG, "v": token}

    def decrypt_json(self, value: Any) -> Any:
        if not (isinstance(value, dict) and value.get("_enc") == _TAG):
            return value
        if self._f is None:
            raise RuntimeError("encrypted data found but CHAT_DATA_ENCRYPTION_KEYS is not set")
        return json.loads(self._f.decrypt(value["v"].encode()))

    def rotate_json(self, value: Any) -> Any:
        if not (isinstance(value, dict) and value.get("_enc") == _TAG) or self._f is None:
            return self.encrypt_json(value)  # plain rows get encrypted with the current key
        return {"_enc": _TAG, "v": self._f.rotate(value["v"].encode()).decode()}


async def rotate_tool_results(engine: AsyncEngine, cipher: FieldCipher, batch: int = 500) -> int:
    """Re-encrypt every stored tool result with the current (first) key. Safe to re-run."""
    from chat_service.db.schema import chat_tool_calls as tc

    if not cipher.enabled:
        raise RuntimeError("set CHAT_DATA_ENCRYPTION_KEYS first")
    done, last_id = 0, None
    while True:
        q = select(tc.c.id, tc.c.result).where(tc.c.result.is_not(None)).order_by(tc.c.id).limit(batch)
        if last_id is not None:
            q = q.where(tc.c.id > last_id)
        async with engine.begin() as conn:
            rows = (await conn.execute(q)).all()
            for row in rows:
                await conn.execute(update(tc).where(tc.c.id == row.id).values(result=cipher.rotate_json(row.result)))
        if not rows:
            return done
        done += len(rows)
        last_id = rows[-1].id


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["genkey", "rotate"])
    args = ap.parse_args()
    if args.command == "genkey":
        print(Fernet.generate_key().decode())
        return
    from chat_service.config import get_settings
    from chat_service.db import create_engine

    settings = get_settings()

    async def go():
        engine = create_engine(settings.database_url)
        try:
            print(f"re-encrypted {await rotate_tool_results(engine, FieldCipher(settings.data_encryption_keys))} rows")
        finally:
            await engine.dispose()

    asyncio.run(go())


if __name__ == "__main__":
    main()
