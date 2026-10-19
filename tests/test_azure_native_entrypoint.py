from __future__ import annotations

import asyncio
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kwikledgers_fastmcp.domains.azure_devops import FastMcpDailySummaryProvider, invoke_azure_capability
from kwikledgers_fastmcp.domains.azure_devops.native import AzureCapabilityError, CAPABILITIES, REVISION_REQUIRED_CAPABILITIES, REVISION_MAP_CAPABILITIES
from kwikledgers_fastmcp.domains.azure_devops.primitives import EXPECTED_REVISIONS


class NativeEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        configuration = patch("kwikledgers_fastmcp.domains.azure_devops.native.load_configuration")
        configuration.start()
        self.addCleanup(configuration.stop)

    def test_full_catalog_has_21_handlers(self) -> None:
        self.assertEqual(len(CAPABILITIES), 23)
        self.assertEqual(sum(operation == "execute" for operation, _ in CAPABILITIES.values()), 9)

    def test_every_handler_normalizes_json_and_text_without_transport(self) -> None:
        for name, (operation, _) in CAPABILITIES.items():
            for raw, expected in (("plain", {"message": "plain"}), ('{"ok": true}', {"ok": True})):
                with self.subTest(name=name, raw=raw):
                    arguments = {}
                    if name in REVISION_REQUIRED_CAPABILITIES:
                        arguments = {"story_id": 1, "expected_revision": 1}
                    elif name in REVISION_MAP_CAPABILITIES:
                        arguments = {"expected_revisions": {"1": 1}}
                    with patch.dict(CAPABILITIES, {name: (operation, lambda **kwargs: raw)}):
                        with patch("kwikledgers_fastmcp.domains.azure_devops.native.load_configuration"):
                            result = asyncio.run(invoke_azure_capability(name, arguments))
                    self.assertEqual(result, {"capability": name, "domain": "azure_devops", "operation": operation, "data": expected})

    def test_active_user_remains_native(self) -> None:
        with patch.dict(os.environ, {"AZURE_USER_EMAIL": "dev@example.com", "AZURE_USER_NAME": "Dev"}):
            with patch("kwikledgers_fastmcp.domains.azure_devops.read_services.load_dotenv"):
                result = asyncio.run(invoke_azure_capability("get_active_user"))
        self.assertEqual(result["data"], {"message": "Usuario ativo: Dev <dev@example.com>"})

    def test_provider_uses_native_summary(self) -> None:
        with patch.dict(CAPABILITIES, {"get_my_daily_summary": ("read", lambda: '{"counts": {"assigned_items": 2}}')}):
            self.assertEqual(FastMcpDailySummaryProvider().fetch_daily_summary()["counts"]["assigned_items"], 2)

    def test_unknown_and_bad_arguments_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(invoke_azure_capability("unknown"))
        with self.assertRaises(TypeError):
            asyncio.run(invoke_azure_capability("get_active_user", {"approved": True}))

    def test_revision_context_is_scoped_to_invocation(self) -> None:
        def handler(story_id: int) -> str:
            self.assertEqual(EXPECTED_REVISIONS.get(), {story_id: 4})
            return "ok"

        with patch.dict(CAPABILITIES, {"update_story_status": ("execute", handler)}):
            asyncio.run(invoke_azure_capability("update_story_status", {"story_id": 12, "expected_revision": 4}))
        self.assertEqual(EXPECTED_REVISIONS.get(), {})

    def test_errors_redact_pat_and_basic_auth(self) -> None:
        from base64 import b64encode

        secret = "private-pat"
        encoded = b64encode(f":{secret}".encode()).decode()
        def handler() -> str:
            raise RuntimeError(f"{secret} {encoded}")

        with patch.dict(os.environ, {"AZURE_PAT": secret}):
            with patch.dict(CAPABILITIES, {"get_open_prs": ("read", handler)}):
                with self.assertRaises(AzureCapabilityError) as raised:
                    asyncio.run(invoke_azure_capability("get_open_prs"))
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(encoded, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_sdk_http_conflict_code_remains_available_to_the_caller(self) -> None:
        from types import SimpleNamespace

        def handler(story_id: int) -> str:
            error = RuntimeError("revision conflict")
            error.response = SimpleNamespace(status_code=412)
            raise error

        with patch.dict(CAPABILITIES, {"update_story_status": ("execute", handler)}):
            with self.assertRaises(AzureCapabilityError) as raised:
                asyncio.run(invoke_azure_capability("update_story_status", {"story_id": 1, "expected_revision": 7}))
        self.assertEqual(raised.exception.status_code, 412)
        self.assertEqual(raised.exception.capability, "update_story_status")

    def test_domain_has_no_legacy_or_transport_imports(self) -> None:
        domain = Path(__file__).parents[1] / "src/kwikledgers_fastmcp/domains/azure_devops"
        for path in domain.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    self.assertFalse(any(part in module for part in ("legacy", "mcp.server", "fastmcp.server", "utils.env", "utils.logger")), (path, module))

    def test_standalone_import_and_invocation_do_not_load_transport_or_change_sys_path(self) -> None:
        code = """
import asyncio
import importlib.abc
import sys

class ForbiddenImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'azure_devops', 'utils', 'mcp', 'fastmcp'} or fullname.startswith('kwikledgers_fastmcp.shared.legacy'):
            raise AssertionError('Forbidden native import: ' + fullname)

sys.meta_path.insert(0, ForbiddenImports())
original_path = list(sys.path)
from kwikledgers_fastmcp.domains.azure_devops import invoke_azure_capability
result = asyncio.run(invoke_azure_capability('get_active_user'))
assert result['data'] == {'message': 'Usuario ativo: Standalone <standalone@example.test>'}, result
assert sys.path == original_path
print('standalone-native-ok')
"""
        source = Path(__file__).resolve().parents[1] / "src"
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "PYTHONPATH": str(source), "KWIKLEDGERS_ROOT_DIR": directory,
                           "KWIKLEDGERS_MCP_SERVERS_PATH": directory, "KWIKLEDGERS_ENV_FILE": str(Path(directory) / "missing.env"),
                           "MCP_ENV_FILE": "", "AZURE_USER_EMAIL": "standalone@example.test", "AZURE_USER_NAME": "Standalone"}
            result = subprocess.run([sys.executable, "-c", code], cwd=directory, env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "standalone-native-ok")