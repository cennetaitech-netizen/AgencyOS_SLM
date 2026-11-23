"""Tool registry: what the model is allowed to call.

Rules (docs/IMPLEMENTATION_GUIDE.md §5.3 and Phase 9):
* Read tools execute directly, with the *user's own* credentials (``ToolContext.user_credential``).
* Write tools can only be registered when write actions are enabled, must declare ``risk_tier`` and a
  ``preview_tool``, and are **never executed by the registry**. The engine turns a write call into a
  proposal that a human must confirm (Phase 9).
* Arguments are validated against the JSON schema before execution; invalid calls are never executed.
* Results are compact JSON, truncated to a size limit.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import jsonschema


@dataclass
class ToolContext:
    org_id: uuid.UUID
    user_id: uuid.UUID
    request_id: str
    roles: tuple[str, ...] = ()
    org_timezone: str = "UTC"
    org_currency: str = "USD"
    user_credential: str | None = field(default=None, repr=False)  # forwarded to the product API


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    label: str = "Working on it"
    group: str = "general"
    read_only: bool = True
    risk_tier: int | None = None
    reversible: bool = False
    preview_tool: str | None = None
    undo_tool: str | None = None

    def schema(self) -> dict[str, Any]:
        """What the model sees. Validation-only keywords are left out (arguments are still validated against
        the full ``parameters``): they cost prompt tokens on every call and don't change what the model writes."""
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": model_view(self.parameters)},
        }


# JSON-schema keywords that only constrain validation. A boolean additionalProperties is dropped; a schema-valued
# one (it describes the values, e.g. validate_ad_copy fields) is kept.
_VALIDATION_ONLY = {
    "additionalProperties",
    "pattern",
    "minLength",
    "maxLength",
    "exclusiveMinimum",
    "minItems",
    "maxItems",
}


def model_view(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _VALIDATION_ONLY and not (key == "additionalProperties" and isinstance(value, dict)):
            continue
        if key == "properties":
            out[key] = {name: model_view(sub) for name, sub in value.items()}
        elif key in ("items", "additionalProperties"):
            out[key] = model_view(value)
        else:
            out[key] = value
    return out


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    truncated: bool = False
    duration_ms: int = 0
    result_chars: int = 0

    def for_model(self) -> str:
        if self.ok:
            return json.dumps(self.data, default=str, separators=(",", ":"))
        return json.dumps({"error": self.error}, separators=(",", ":"))


class ToolRegistryError(Exception):
    pass


class ToolRegistry:
    def __init__(self, allow_write: bool = False) -> None:
        self._tools: dict[str, Tool] = {}
        self.allow_write = allow_write

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ToolRegistryError(f"duplicate tool {tool.name!r}")
        if not tool.read_only:
            if not self.allow_write:
                raise ToolRegistryError(
                    f"write tool {tool.name!r} rejected: write actions are disabled (read-only mode)"
                )
            if tool.risk_tier not in (1, 2, 3) or not tool.preview_tool:
                raise ToolRegistryError(f"write tool {tool.name!r} must declare risk_tier and preview_tool")
        jsonschema.Draft202012Validator.check_schema(tool.parameters)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, include_write: bool = False) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values() if t.read_only or include_write]

    async def execute(
        self,
        ctx: ToolContext,
        name: str,
        raw_args: str | dict[str, Any] | None,
        timeout_seconds: float,
        max_chars: int,
    ) -> ToolResult:
        start = time.monotonic()

        def done(**kw) -> ToolResult:
            return ToolResult(duration_ms=int((time.monotonic() - start) * 1000), **kw)

        tool = self._tools.get(name)
        if tool is None:
            return done(ok=False, error=f"unknown tool {name!r}")
        if not tool.read_only:
            return done(ok=False, error="write tools execute only through the confirmation flow")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
            jsonschema.validate(args, tool.parameters)
        except (ValueError, jsonschema.ValidationError) as exc:
            msg = exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc)
            return done(ok=False, error=f"invalid arguments: {msg}")
        try:
            data = await asyncio.wait_for(tool.handler(ctx, args), timeout=timeout_seconds)
        except TimeoutError:
            return done(ok=False, error="the data source took too long to respond")
        except PermissionError:
            return done(ok=False, error="permission denied for this user")
        except ToolInputError as exc:
            return done(ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - tool failures are reported to the model, not raised
            return done(ok=False, error=f"tool failed: {type(exc).__name__}")
        data, truncated = _fit(data, max_chars)
        if data is None:
            return done(ok=False, error="result too large; narrow the request (date range, filters)")
        text = json.dumps(data, default=str, separators=(",", ":"))
        return done(ok=True, data=data, truncated=truncated, result_chars=len(text))


class ToolInputError(Exception):
    """Raised by handlers for user-fixable problems (e.g. unknown campaign ID)."""


def _fit(data: dict[str, Any], max_chars: int) -> tuple[dict[str, Any] | None, bool]:
    def size(d: Any) -> int:
        return len(json.dumps(d, default=str, separators=(",", ":")))

    if size(data) <= max_chars:
        return data, False
    trimmed = dict(data)
    rows = trimmed.get("rows")
    if isinstance(rows, list):
        rows = list(rows)
        while rows and size({**trimmed, "rows": rows, "truncated": True}) > max_chars:
            rows.pop()
        trimmed["rows"] = rows
        trimmed["truncated"] = True
        if size(trimmed) <= max_chars:
            return trimmed, True
    return None, True
