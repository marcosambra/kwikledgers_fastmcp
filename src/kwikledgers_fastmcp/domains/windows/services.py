from __future__ import annotations

from typing import Any

from kwikledgers_fastmcp.shared.legacy_runtime import ensure_legacy_import_path


class FastMcpNotificationAdapter:
    """Windows notification bridge for the FastMCP daily_summary slice."""

    def send_daily_summary_notification(self, summary: dict[str, Any]) -> Any:
        ensure_legacy_import_path()
        from windows_calendar import server as windows_server

        counts = summary.get("counts", {})
        blocked_items = counts.get("blocked_items", 0)
        urgency = "high" if blocked_items else "normal"
        remaining_story_points = summary.get("remaining_story_points", "-")
        message = (
            "Resumo diario sincronizado pelo runtime LangGraph via FastMCP. "
            f"Itens atribuidos: {counts.get('assigned_items', 0)}. "
            f"Bloqueados: {blocked_items}. "
            f"PRs abertas: {counts.get('open_prs', 0)}. "
            f"Story points restantes: {remaining_story_points}."
        )
        return windows_server._send_notification(
            "Resumo diario disponivel",
            message,
            urgency,
        )