from __future__ import annotations

import json
from typing import Any

from kwikledgers_fastmcp.shared.legacy_runtime import ensure_legacy_import_path


class FastMcpTrackingSyncAdapter:
    """Local tracking bridge for the first FastMCP daily_summary slice."""

    def sync_daily_tracking(self, summary: dict[str, Any], source: str) -> Any:
        ensure_legacy_import_path()
        from local_tracking import server as tracking_server

        return tracking_server._sync_daily_tracking(
            json.dumps(summary, ensure_ascii=True),
            [
                "langgraph_runtime",
                "fastmcp_daily_summary_provider",
                "fastmcp_local_tracking_adapter",
            ],
            "Resumo diario executado pelo runtime LangGraph via FastMCP.",
            None,
            "Resumo diario executado pelo runtime LangGraph via FastMCP.",
            None,
            source,
        )