from __future__ import annotations

from typing import Any

from kwikledgers_fastmcp.shared.legacy_runtime import ensure_legacy_import_path, parse_legacy_json_dict


class FastMcpDailySummaryProvider:
    """Read-only compatibility bridge for the first FastMCP Azure capability."""

    def fetch_daily_summary(self) -> dict[str, Any]:
        ensure_legacy_import_path()
        from azure_devops import server as azure_server

        payload = azure_server._get_my_daily_summary()
        return parse_legacy_json_dict(
            payload,
            invalid_message="O provider FastMCP de resumo diario retornou JSON invalido.",
            root_message="O provider FastMCP de resumo diario retornou um payload raiz invalido.",
        )