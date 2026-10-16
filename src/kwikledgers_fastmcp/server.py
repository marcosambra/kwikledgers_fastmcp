from __future__ import annotations

import json
from typing import Any

from fastmcp import Context, FastMCP

from kwikledgers_fastmcp.domains.azure_devops import FastMcpDailySummaryProvider
from kwikledgers_fastmcp.domains.local_tracking import FastMcpTrackingSyncAdapter
from kwikledgers_fastmcp.domains.windows import FastMcpNotificationAdapter
from kwikledgers_fastmcp.shared.capability_gateway import (
    CAPABILITY_CATALOG,
    CapabilityDefinition,
    invoke_capability,
)
from kwikledgers_fastmcp.shared.operational_context import OperationalContext, bind_operational_context


def build_server_metadata() -> dict[str, Any]:
    return {
        "name": "kwikledgers-fastmcp",
        "status": "active_daily_summary_transport",
        "domains": ["azure_devops", "local_tracking", "windows"],
        "active_runtime_path": "kwikledgers_langgraph",
        "capabilities": {
            definition.name: f"active_{definition.operation}_capability"
            for definition in CAPABILITY_CATALOG
        },
    }


def build_daily_summary_provider() -> FastMcpDailySummaryProvider:
    return FastMcpDailySummaryProvider()


def build_tracking_sync_adapter() -> FastMcpTrackingSyncAdapter:
    return FastMcpTrackingSyncAdapter()


def build_notification_adapter() -> FastMcpNotificationAdapter:
    return FastMcpNotificationAdapter()


def get_my_daily_summary(
    provider: FastMcpDailySummaryProvider | None = None,
) -> dict[str, Any]:
    """Return the normalized Azure daily summary without workflow side effects."""
    return (provider or build_daily_summary_provider()).fetch_daily_summary()


def build_fastmcp_server() -> FastMCP:
    mcp = FastMCP(
        "kwikledgers-fastmcp",
        instructions=(
            "Expose only atomic KwikLedgers integration capabilities. "
            "Workflow sequencing, approval, replay, and governance are owned by LangGraph."
        ),
    )

    for definition in CAPABILITY_CATALOG:
        mcp.tool(
            name=definition.name,
            description=(
                f"[{definition.domain}/{definition.operation}] {definition.description} "
                "Regras de processo, aprovação e replay pertencem ao LangGraph."
            ),
        )(_build_capability_handler(definition))

    return mcp


def _build_capability_handler(definition: CapabilityDefinition) -> Any:
    async def capability_handler(ctx: Context, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        request = ctx.request_context
        metadata = request.meta.model_dump() if request and request.meta else {}
        with bind_operational_context(OperationalContext.from_metadata(metadata)):
            return await invoke_capability(definition, arguments)

    capability_handler.__name__ = definition.name
    capability_handler.__doc__ = definition.description
    return capability_handler


def main() -> int:
    build_fastmcp_server().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())