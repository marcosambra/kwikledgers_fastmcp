from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import patch

from kwikledgers_fastmcp.server import (
    build_daily_summary_provider,
    build_fastmcp_server,
    build_notification_adapter,
    build_server_metadata,
    build_tracking_sync_adapter,
    get_my_daily_summary,
)
from kwikledgers_fastmcp.shared.legacy_gateway import (
    CAPABILITY_CATALOG,
    CapabilityDefinition,
    invoke_legacy_capability,
)


class FastMcpDailySummaryProviderTestCase(unittest.TestCase):
    def test_fetch_daily_summary_reads_legacy_payload(self) -> None:
        provider = build_daily_summary_provider()

        with patch("kwikledgers_fastmcp.domains.azure_devops.read_services.ensure_legacy_import_path"):
            fake_package = types.ModuleType("azure_devops")
            fake_server = types.ModuleType("azure_devops.server")
            fake_server._get_my_daily_summary = lambda: '{"counts": {"assigned_items": 2, "blocked_items": 1}}'
            fake_package.server = fake_server
            with patch.dict(sys.modules, {"azure_devops": fake_package, "azure_devops.server": fake_server}):
                result = provider.fetch_daily_summary()

        self.assertEqual(result["counts"]["assigned_items"], 2)

    def test_fetch_daily_summary_rejects_invalid_json(self) -> None:
        provider = build_daily_summary_provider()

        with patch("kwikledgers_fastmcp.domains.azure_devops.read_services.ensure_legacy_import_path"):
            fake_package = types.ModuleType("azure_devops")
            fake_server = types.ModuleType("azure_devops.server")
            fake_server._get_my_daily_summary = lambda: "{"
            fake_package.server = fake_server
            with patch.dict(sys.modules, {"azure_devops": fake_package, "azure_devops.server": fake_server}):
                with self.assertRaises(RuntimeError):
                    provider.fetch_daily_summary()


class FastMcpServerTestCase(unittest.TestCase):
    def test_build_server_metadata_reports_active_read_only_transport(self) -> None:
        metadata = build_server_metadata()

        self.assertEqual(metadata["name"], "kwikledgers-fastmcp")
        self.assertEqual(metadata["status"], "active_daily_summary_transport")
        self.assertEqual(metadata["domains"], ["azure_devops", "local_tracking", "windows"])
        self.assertEqual(metadata["active_runtime_path"], "kwikledgers_langgraph")
        self.assertEqual(metadata["capabilities"]["get_my_daily_summary"], "active_read_capability")

    def test_build_fastmcp_server_creates_transport_instance(self) -> None:
        server = build_fastmcp_server()

        self.assertEqual(server.name, "kwikledgers-fastmcp")

    def test_build_fastmcp_server_registers_the_full_capability_catalog(self) -> None:
        tools = asyncio.run(build_fastmcp_server().get_tools())

        self.assertEqual(set(tools), {definition.name for definition in CAPABILITY_CATALOG})

    def test_get_my_daily_summary_delegates_to_the_provider(self) -> None:
        class FakeProvider:
            def fetch_daily_summary(self) -> dict[str, Any]:
                return {"counts": {"assigned_items": 3}}

        result = get_my_daily_summary(FakeProvider())

        self.assertEqual(result["counts"]["assigned_items"], 3)


class LegacyGatewayTestCase(unittest.TestCase):
    def test_invoke_legacy_capability_normalizes_json_result(self) -> None:
        definition = CapabilityDefinition("get_my_daily_summary", "azure_devops", "read", "test")
        fake_package = types.ModuleType("azure_devops")
        fake_server = types.ModuleType("azure_devops.server")

        async def dispatch(name: str, arguments: dict[str, object]) -> str:
            self.assertEqual(name, "get_my_daily_summary")
            self.assertEqual(arguments, {})
            return '{"counts":{"assigned_items":2}}'

        fake_server._dispatch = dispatch
        fake_package.server = fake_server
        with patch("kwikledgers_fastmcp.shared.legacy_gateway.ensure_legacy_import_path"):
            with patch.dict(sys.modules, {"azure_devops": fake_package, "azure_devops.server": fake_server}):
                result = asyncio.run(invoke_legacy_capability(definition))

        self.assertEqual(result["domain"], "azure_devops")
        self.assertEqual(result["data"]["counts"]["assigned_items"], 2)

    def test_invoke_legacy_capability_rejects_workflow_fields(self) -> None:
        definition = CapabilityDefinition("get_my_daily_summary", "azure_devops", "read", "test")

        with self.assertRaises(ValueError):
            asyncio.run(invoke_legacy_capability(definition, {"approved": True}))


class FastMcpTrackingSyncAdapterTestCase(unittest.TestCase):
    def test_sync_daily_tracking_calls_legacy_tracking_server(self) -> None:
        adapter = build_tracking_sync_adapter()

        with patch("kwikledgers_fastmcp.domains.local_tracking.services.ensure_legacy_import_path"):
            fake_package = types.ModuleType("local_tracking")
            fake_server = types.ModuleType("local_tracking.server")

            def fake_sync(
                summary: str,
                actions_taken: list[str],
                log_title: str | None,
                log_details: str | None,
                metrics_title: str | None,
                metrics_summary: str | None,
                source: str,
            ) -> str:
                self.assertIn("fastmcp_local_tracking_adapter", actions_taken)
                self.assertEqual(source, "test-source")
                self.assertIn('"assigned_items": 2', summary)
                self.assertEqual(log_title, "Resumo diario executado pelo runtime LangGraph via FastMCP.")
                self.assertEqual(metrics_title, "Resumo diario executado pelo runtime LangGraph via FastMCP.")
                return "tracking synced"

            fake_server._sync_daily_tracking = fake_sync
            fake_package.server = fake_server
            with patch.dict(sys.modules, {"local_tracking": fake_package, "local_tracking.server": fake_server}):
                result = adapter.sync_daily_tracking({"counts": {"assigned_items": 2}}, "test-source")

        self.assertEqual(result, "tracking synced")


class FastMcpNotificationAdapterTestCase(unittest.TestCase):
    def test_send_daily_summary_notification_calls_legacy_windows_server(self) -> None:
        adapter = build_notification_adapter()

        with patch("kwikledgers_fastmcp.domains.windows.services.ensure_legacy_import_path"):
            fake_package = types.ModuleType("windows_calendar")
            fake_server = types.ModuleType("windows_calendar.server")

            def fake_send_notification(title: str, message: str, urgency: str = "normal") -> str:
                self.assertEqual(title, "Resumo diario disponivel")
                self.assertEqual(urgency, "high")
                self.assertIn("FastMCP", message)
                self.assertIn("Itens atribuidos: 3", message)
                self.assertIn("Bloqueados: 1", message)
                return "notification sent"

            fake_server._send_notification = fake_send_notification
            fake_package.server = fake_server
            with patch.dict(sys.modules, {"windows_calendar": fake_package, "windows_calendar.server": fake_server}):
                result = adapter.send_daily_summary_notification(
                    {"counts": {"assigned_items": 3, "blocked_items": 1, "open_prs": 2}}
                )

        self.assertEqual(result, "notification sent")


if __name__ == "__main__":
    unittest.main()