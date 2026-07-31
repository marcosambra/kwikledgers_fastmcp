from __future__ import annotations

import json
from typing import Any

from kwikledgers_fastmcp.domains.azure_devops import FastMcpDailySummaryProvider
from kwikledgers_fastmcp.domains.local_tracking import FastMcpTrackingSyncAdapter
from kwikledgers_fastmcp.domains.windows import FastMcpNotificationAdapter


def build_server_metadata() -> dict[str, Any]:
    return {
        "name": "kwikledgers-fastmcp",
        "status": "paused_partial_implementation",
        "domains": ["azure_devops", "local_tracking", "windows"],
        "active_runtime_path": "kwikledgers_langgraph",
        "capabilities": {
            "daily_summary": "frozen_legacy_compatible_slice",
            "story_pull_request": "planned",
            "story_branch_association": "planned",
            "story_status_transition": "planned",
        },
    }


def build_daily_summary_provider() -> FastMcpDailySummaryProvider:
    return FastMcpDailySummaryProvider()


def build_tracking_sync_adapter() -> FastMcpTrackingSyncAdapter:
    return FastMcpTrackingSyncAdapter()


def build_notification_adapter() -> FastMcpNotificationAdapter:
    return FastMcpNotificationAdapter()


def main() -> int:
    print(json.dumps(build_server_metadata(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())