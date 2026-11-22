"""Tools the model can call."""

from __future__ import annotations

from chat_service.config import Settings
from chat_service.tools.builtin.ad_copy import VALIDATE_AD_COPY
from chat_service.tools.builtin.media_math import MEDIA_MATH
from chat_service.tools.product import HttpProductAPI, MockProductAPI, ProductAPI, product_tools
from chat_service.tools.registry import ToolRegistry


def build_registry(settings: Settings, product_api: ProductAPI | None = None) -> ToolRegistry:
    registry = ToolRegistry(allow_write=False)  # Phase 9 enables write tools via the confirmation flow
    if product_api is None:
        product_api = (
            MockProductAPI() if settings.product_adapter == "mock" else HttpProductAPI(settings.product_api_base_url)
        )
    for tool in [MEDIA_MATH, VALIDATE_AD_COPY, *product_tools(product_api)]:
        registry.register(tool)
    return registry
