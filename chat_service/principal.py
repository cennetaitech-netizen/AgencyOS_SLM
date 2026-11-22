"""The authenticated user making a request."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    org_id: uuid.UUID
    display_name: str = "User"
    roles: tuple[str, ...] = ()
    credential: str | None = field(default=None, repr=False)  # forwarded to the product API as the user

    @property
    def primary_role(self) -> str:
        return self.roles[0] if self.roles else "member"

    def has_role(self, role: str) -> bool:
        return role in self.roles
