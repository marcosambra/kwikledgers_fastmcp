from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from kwikledgers_fastmcp.server import (
    build_daily_summary_provider,
    build_fastmcp_server,
    build_notification_adapter,
    build_server_metadata,
    build_tracking_sync_adapter,
    get_my_daily_summary,
)
from kwikledgers_fastmcp.shared.capability_gateway import (
    CAPABILITY_CATALOG,
    CapabilityDefinition,
    invoke_capability,
)


class FastMcpDailySummaryProviderTestCase(unittest.TestCase):
    def test_fetch_daily_summary_reads_native_payload(self) -> None:
        provider = build_daily_summary_provider()

        with patch("kwikledgers_fastmcp.domains.azure_devops.native._invoke", return_value={
            "data": {"counts": {"assigned_items": 2, "blocked_items": 1}},
        }):
            result = provider.fetch_daily_summary()

        self.assertEqual(result["counts"]["assigned_items"], 2)

    def test_fetch_daily_summary_rejects_invalid_native_payload(self) -> None:
        provider = build_daily_summary_provider()

        with patch("kwikledgers_fastmcp.domains.azure_devops.native._invoke", return_value={
            "data": {"message": "payload inválido"},
        }):
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


class CapabilityGatewayTestCase(unittest.TestCase):
    def test_active_user_uses_native_dispatch(self) -> None:
        definition = CapabilityDefinition("get_active_user", "azure_devops", "read", "test")

        with patch("kwikledgers_fastmcp.domains.azure_devops.read_services.load_configuration"):
            with patch.dict(os.environ, {"AZURE_USER_EMAIL": "dev.user@example.com", "AZURE_USER_NAME": "Dev User"}):
                result = asyncio.run(invoke_capability(definition))

        self.assertEqual(result["data"], {"message": "Usuario ativo: Dev User <dev.user@example.com>"})

    def test_active_user_loads_explicit_environment_file(self) -> None:
        definition = CapabilityDefinition("get_active_user", "azure_devops", "read", "test")
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment_path = Path(temporary_directory) / ".env"
            environment_path.write_text(
                "AZURE_USER_EMAIL=dev.user@example.com\nAZURE_USER_NAME=Dev User\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {
                "KWIKLEDGERS_ENV_FILE": str(environment_path),
            }, clear=True):
                result = asyncio.run(invoke_capability(definition))

        self.assertEqual(result["data"], {"message": "Usuario ativo: Dev User <dev.user@example.com>"})

    def test_active_user_uses_git_email_and_normalizes_name(self) -> None:
        definition = CapabilityDefinition("get_active_user", "azure_devops", "read", "test")
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"KWIKLEDGERS_ROOT_DIR": temporary_directory}, clear=True):
                with patch("kwikledgers_fastmcp.domains.azure_devops.read_services.subprocess.run", side_effect=[
                    types.SimpleNamespace(stdout="dev._user@example.com\n"),
                    types.SimpleNamespace(stdout=""),
                    types.SimpleNamespace(stdout=""),
                ]):
                    result = asyncio.run(invoke_capability(definition))

        self.assertEqual(result["data"], {"message": "Usuario ativo: Dev User <dev._user@example.com>"})

    def test_active_user_reports_missing_email(self) -> None:
        definition = CapabilityDefinition("get_active_user", "azure_devops", "read", "test")
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"KWIKLEDGERS_ROOT_DIR": temporary_directory}, clear=True):
                with patch("kwikledgers_fastmcp.domains.azure_devops.read_services.subprocess.run", return_value=types.SimpleNamespace(stdout="")):
                    result = asyncio.run(invoke_capability(definition))

        self.assertEqual(result["data"], {
            "message": "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"
        })

    def test_invoke_capability_normalizes_json_result(self) -> None:
        definition = CapabilityDefinition("get_my_daily_summary", "azure_devops", "read", "test")

        async def dispatch(name: str, arguments: dict[str, object]) -> str:
            self.assertEqual(name, "get_my_daily_summary")
            self.assertEqual(arguments, {})
            return {
                "capability": name,
                "domain": "azure_devops",
                "operation": "read",
                "data": {"counts": {"assigned_items": 2}},
            }

        with patch("kwikledgers_fastmcp.shared.capability_gateway.invoke_azure_capability", new=dispatch):
            result = asyncio.run(invoke_capability(definition))

        self.assertEqual(result["domain"], "azure_devops")
        self.assertEqual(result["data"]["counts"]["assigned_items"], 2)

    def test_invoke_capability_rejects_workflow_fields(self) -> None:
        definition = CapabilityDefinition("get_my_daily_summary", "azure_devops", "read", "test")

        with self.assertRaises(ValueError):
            asyncio.run(invoke_capability(definition, {"approved": True}))

    def test_execute_capability_requires_signed_langgraph_token(self) -> None:
        definition = CapabilityDefinition("update_story_status", "azure_devops", "execute", "test")

        with self.assertRaises(RuntimeError):
            asyncio.run(invoke_capability(definition, {"story_id": 1, "status": "Closed"}))

    def test_execute_capability_allows_signed_matching_payload(self) -> None:
        definition = CapabilityDefinition("update_story_status", "azure_devops", "execute", "test")
        arguments = {"story_id": 1, "status": "Closed", "expected_revision": 1}
        token = self._build_execution_token(definition.name, arguments)

        async def dispatch(name: str, received_arguments: dict[str, object]) -> dict[str, object]:
            self.assertEqual(name, "update_story_status")
            self.assertEqual(received_arguments, arguments)
            return {
                "capability": name,
                "domain": "azure_devops",
                "operation": "execute",
                "data": {"status": "updated"},
            }

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {
                "KWIKLEDGERS_FASTMCP_EXECUTION_KEY": "test-key",
                "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER": str(Path(temporary_directory) / "ledger.sqlite3"),
            }, clear=False):
                with patch("kwikledgers_fastmcp.shared.capability_gateway.invoke_azure_capability", new=dispatch):
                    result = asyncio.run(
                        invoke_capability(
                            definition,
                            {**arguments, "execution_capability": token},
                        )
                    )

        self.assertEqual(result["data"]["status"], "updated")

    def _build_execution_token(self, capability_name: str, arguments: dict[str, object]) -> str:
        serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        now = datetime.now(timezone.utc)
        payload = {
            "capability": capability_name,
            "payload_digest": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "run_id": "test-run",
            "request_id": "test-request",
            "nonce": uuid4().hex,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=4)).isoformat(),
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encoded_payload = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
        signature = hmac.new(b"test-key", payload_bytes, hashlib.sha256).hexdigest()
        return f"{encoded_payload}.{signature}"


class FastMcpTrackingSyncAdapterTestCase(unittest.TestCase):
    def test_sync_daily_tracking_calls_native_tracking_service(self) -> None:
        adapter = build_tracking_sync_adapter()

        with patch("kwikledgers_fastmcp.domains.local_tracking.services.TrackingService.invoke", return_value="tracking synced") as invoke:
            result = adapter.sync_daily_tracking({"counts": {"assigned_items": 2}}, "test-source")

        self.assertEqual(result, "tracking synced")
        name, arguments = invoke.call_args.args
        self.assertEqual(name, "sync_daily_tracking")
        self.assertIn("fastmcp_local_tracking_adapter", arguments["actions_taken"])
        self.assertEqual(arguments["source"], "test-source")


class FastMcpNotificationAdapterTestCase(unittest.TestCase):
    def test_send_daily_summary_notification_calls_native_windows_service(self) -> None:
        adapter = build_notification_adapter()

        with patch("kwikledgers_fastmcp.domains.windows.services.WindowsService.send_notification", return_value="notification sent") as send:
            result = adapter.send_daily_summary_notification(
                {"counts": {"assigned_items": 3, "blocked_items": 1, "open_prs": 2}}
            )

        self.assertEqual(result, "notification sent")
        title, message, urgency = send.call_args.args
        self.assertEqual(title, "Resumo diario disponivel")
        self.assertEqual(urgency, "high")
        self.assertIn("FastMCP", message)


if __name__ == "__main__":
    unittest.main()