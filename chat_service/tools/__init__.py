"""Tools the model can call."""

from __future__ import annotations

from chat_service.config import Settings
from chat_service.tools.builtin.ad_copy import VALIDATE_AD_COPY
from chat_service.tools.builtin.media_math import MEDIA_MATH
from chat_service.tools.product import HttpProductAPI, MockProductAPI, ProductAPI, product_tools
from chat_service.tools.registry import ToolRegistry


def default_product_api(settings: Settings) -> ProductAPI:
    if settings.product_adapter == "mock":
        return MockProductAPI()
    return HttpProductAPI(settings.product_api_base_url)


def build_registry(
    settings: Settings, product_api: ProductAPI | None = None, include_actions: bool = True
) -> ToolRegistry:
    """Read tools always; write actions registered as schemas only.

    The registry never executes write tools, and the engine only shows them to the model when chat
    actions are enabled for the org. Execution happens in ActionService after human confirmation.
    """
    from chat_service.actions.service import action_tools  # local import: avoids an import cycle
    from chat_service.actions.specs import ACTION_SPECS

    registry = ToolRegistry(allow_write=include_actions)
    product_api = product_api or default_product_api(settings)
    for tool in [MEDIA_MATH, VALIDATE_AD_COPY, *product_tools(product_api)]:
        registry.register(tool)
    if include_actions:
        for tool in action_tools(ACTION_SPECS):
            registry.register(tool)
    return registry
