from __future__ import annotations

import asyncio
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from kwikledgers_fastmcp.domains.local_tracking.services import (
    FastMcpTrackingSyncAdapter, TrackingService, invoke_tracking_capability, resolve_workspace_root,
)


class NativeTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.service = TrackingService(self.root)
        clock = patch("kwikledgers_fastmcp.domains.local_tracking.services.datetime")
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.utcnow.return_value = datetime(2026, 9, 29, 12, 30)

    def test_root_discovery_without_legacy_agent(self) -> None:
        (self.root / ".vscode").mkdir()
        (self.root / "AGENTS.md").write_text("# Native workspace\n", encoding="utf-8")
        service_path = self.root / "kwikledgers_fastmcp/src/domains/local_tracking/services.py"
        with patch.dict(os.environ), patch(
            "kwikledgers_fastmcp.domains.local_tracking.services.__file__", service_path,
        ):
            os.environ.pop("KWIKLEDGERS_ROOT_DIR", None)
            self.assertEqual(resolve_workspace_root(), self.root.resolve())
            (self.root / "AGENTS.md").unlink()
            (self.root / "kwikledgers_fastmcp").mkdir()
            (self.root / "kwikledgers_langgraph").mkdir()
            self.assertEqual(resolve_workspace_root(), self.root.resolve())
            explicit = self.root / "explicit"
            os.environ["KWIKLEDGERS_ROOT_DIR"] = str(explicit)
            self.assertEqual(resolve_workspace_root(), explicit.resolve())
            os.environ.pop("KWIKLEDGERS_ROOT_DIR")
            (self.root / "kwikledgers_langgraph").rmdir()
            with self.assertRaisesRegex(RuntimeError, "KWIKLEDGERS_ROOT_DIR"):
                resolve_workspace_root()
        self.assertFalse((self.root / "agent").exists())

    def test_snapshot_replacements_are_atomic_per_file(self) -> None:
        self.service.update_task_control('{"items": [{"id": 1, "state": "QA"}]}')
        json_path = self.service.task_control_dir / "current-sprint.json"
        md_path = self.service.task_control_dir / "current-sprint.md"
        previous = {path: path.read_bytes() for path in (json_path, md_path)}
        replace = os.replace

        def inspect_replacement(source: Path, destination: Path) -> None:
            self.assertEqual(source.parent, destination.parent)
            self.assertEqual(destination.read_bytes(), previous[destination])
            content = source.read_text(encoding="utf-8")
            if destination == json_path:
                self.assertEqual(json.loads(content)["items"][0]["state"], "Active")
            else:
                self.assertIn("QA -> Active", content)
            replace(source, destination)

        with patch(
            "kwikledgers_fastmcp.domains.local_tracking.services.os.replace",
            side_effect=inspect_replacement,
        ) as replacements, patch(
            "kwikledgers_fastmcp.domains.local_tracking.services.os.fsync", wraps=os.fsync,
        ) as fsync:
            self.service.update_task_control('{"items": [{"id": 1, "state": "Active"}]}')
        self.assertEqual(replacements.call_count, 2)
        self.assertEqual(fsync.call_count, 2)
        self.assertEqual(list(self.service.task_control_dir.glob("*.tmp")), [])

    def test_failed_atomic_replacement_preserves_snapshot_and_cleans_tempfile(self) -> None:
        self.service.update_task_control("{}")
        paths = [self.service.task_control_dir / name for name in ("current-sprint.json", "current-sprint.md")]
        previous = [path.read_bytes() for path in paths]
        with patch(
            "kwikledgers_fastmcp.domains.local_tracking.services.os.replace",
            side_effect=OSError("replacement failed"),
        ), self.assertRaisesRegex(OSError, "replacement failed"):
            self.service.update_task_control('{"items": []}')
        self.assertEqual([path.read_bytes() for path in paths], previous)
        self.assertEqual(list(self.service.task_control_dir.glob("*.tmp")), [])

    def test_invoke_locks_reads_and_releases_after_handler_error(self) -> None:
        with patch.object(self.service, "read_tracking_snapshot", side_effect=ValueError("handler failed")):
            with self.assertRaisesRegex(ValueError, "handler failed"):
                self.service.invoke("read_tracking_snapshot", {})
        lock_path = self.root / "AI_Tracking/Audit/tracking.lock"
        self.assertTrue(lock_path.is_file())
        if os.name != "nt":
            import fcntl

            with lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    pass
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assertIsInstance(self.service.invoke("read_tracking_snapshot", {}), str)
        self.assertTrue(lock_path.is_file())
        self.assertEqual(self.service.invoke("missing", {}), "Ferramenta desconhecida: missing")

    def test_windows_lock_failure_does_not_invoke_handler(self) -> None:
        msvcrt = Mock(LK_LOCK=1, LK_UNLCK=0)
        msvcrt.locking.side_effect = OSError("lock unavailable")
        with patch.dict(sys.modules, {"msvcrt": msvcrt}), patch(
            "kwikledgers_fastmcp.domains.local_tracking.services.os.name", "nt",
        ), patch.object(self.service, "read_tracking_snapshot") as handler:
            with self.assertRaisesRegex(RuntimeError, "Unable to acquire tracking filesystem lock"):
                self.service.invoke("read_tracking_snapshot", {})
        handler.assert_not_called()
        self.assertEqual(msvcrt.locking.call_count, 1)

    def test_windows_lock_releases_after_handler_error(self) -> None:
        msvcrt = Mock(LK_LOCK=1, LK_UNLCK=0)
        with patch.dict(sys.modules, {"msvcrt": msvcrt}), patch(
            "kwikledgers_fastmcp.domains.local_tracking.services.os.name", "nt",
        ), patch.object(self.service, "read_tracking_snapshot", side_effect=ValueError("handler failed")):
            with self.assertRaisesRegex(ValueError, "handler failed"):
                self.service.invoke("read_tracking_snapshot", {})
        self.assertEqual([call.args[1:] for call in msvcrt.locking.call_args_list], [(1, 1), (0, 1)])

    def test_snapshot_exact_paths_and_flags(self) -> None:
        snapshot = json.loads(self.service.read_tracking_snapshot())
        paths = {
            "task_control": "Task_Control/current-sprint.json",
            "task_control_markdown": "Task_Control/current-sprint.md",
            "daily_log": "Daily_Action_Logs/2026-09-29.md",
            "metrics_log": "Metrics/ai-usage-log.md",
            "sprint_metrics": "Metrics/sprint-metrics.md",
        }
        self.assertEqual(snapshot, {
            **{key: str(self.root / "AI_Tracking" / path) for key, path in paths.items()},
            "exists": dict.fromkeys(paths, False),
        })
        self.assertTrue(self.service.metrics_dir.is_dir())

    def test_state_change_detection_and_json_contract(self) -> None:
        self.service.update_task_control('{"items": [{"id": 12, "state": "QA"}]}')
        result = self.service.update_task_control(json.dumps({"items": [
            {"id": 12, "title": "Correção", "state": " Desenvolvimento "},
            {"id": 13, "state": "Active"},
        ]}))
        payload = json.loads((self.service.task_control_dir / "current-sprint.json").read_text())
        self.assertEqual(payload["returned_items"], [{
            "id": 12, "title": "Correção", "previous_state": "QA", "current_state": "Desenvolvimento",
        }])
        self.assertIn("Itens devolvidos detectados: 1", result)
        self.assertIn("Corre\\u00e7\\u00e3o", (self.service.task_control_dir / "current-sprint.json").read_text())
        self.assertIn("QA -> Desenvolvimento", (self.service.task_control_dir / "current-sprint.md").read_text())

    def test_logs_append_with_exact_header_and_utc(self) -> None:
        self.service.append_daily_action_log("Primeiro", "Detalhes")
        self.service.append_daily_action_log("Segundo", "Outra ação", "developer")
        self.assertEqual((self.service.daily_log_dir / "2026-09-29.md").read_text(),
            "# Daily Action Log\n\nData: 2026-09-29\n"
            "Escopo: atividade operacional do agente e do desenvolvedor\n\n## Entradas\n\n"
            "### 2026-09-29 12:30:00Z - Primeiro\nFonte: agent\n\nDetalhes\n\n"
            "### 2026-09-29 12:30:00Z - Segundo\nFonte: developer\n\nOutra ação\n\n")

    def test_metrics_append_nullable_fields_and_table_pipe(self) -> None:
        self.service.record_ai_metrics("Uso", "A | B", ["Teste"])
        self.service.record_ai_metrics("Uso 2", "Resumo", [], 0, 0)
        usage = (self.service.metrics_dir / "ai-usage-log.md").read_text()
        self.assertEqual(usage.count("# AI Usage Metrics Log"), 1)
        self.assertIn("Resumo: A | B\nAcoes executadas:\n- Teste\n\n", usage)
        self.assertIn("Story points restantes: 0\nItens bloqueados: 0", usage)
        self.assertIn("| 2026-09-29 12:30:00Z | - | - | A / B |\n",
                      (self.service.metrics_dir / "sprint-metrics.md").read_text())

    def test_sync_defaults_preserve_original_summary_for_logs(self) -> None:
        self.service.update_task_control('{"items": [{"id": 1, "state": "QA"}]}')
        result = self.service.sync_daily_tracking(
            '{"items": [{"id": 1, "state": "Active"}], "counts": {"blocked_items": 2}}', ["Teste"],
        )
        self.assertEqual(len(result.splitlines()), 3)
        self.assertIn("Itens devolvidos detectados: 1", result)
        self.assertIn("Itens devolvidos detectados: 0.",
                      (self.service.daily_log_dir / "2026-09-29.md").read_text())
        self.assertTrue(all(json.loads(self.service.read_tracking_snapshot())["exists"].values()))

    def test_invalid_summary_and_corrupt_existing_snapshot(self) -> None:
        for summary in ("[]", "null", '"text"'):
            with self.assertRaisesRegex(ValueError, "esperado objeto JSON"):
                self.service.update_task_control(summary)
        with self.assertRaises(json.JSONDecodeError):
            self.service.update_task_control("{")
        self.service.update_task_control("{}")
        (self.service.task_control_dir / "current-sprint.json").write_text("{")
        with self.assertRaises(json.JSONDecodeError):
            self.service.update_task_control("{}")

    def test_async_dispatch_and_native_adapter(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_ROOT_DIR": str(self.root)}):
            for name, arguments in (
                ("update_task_control", {"summary": "{}"}),
                ("append_daily_action_log", {"title": "Log", "details": "Details"}),
                ("record_ai_metrics", {"entry_title": "AI", "summary": "Usage", "actions_taken": []}),
                ("sync_daily_tracking", {"summary": "{}", "actions_taken": []}),
                ("read_tracking_snapshot", {}),
            ):
                self.assertIsInstance(asyncio.run(invoke_tracking_capability(name, arguments)), str)
            result = FastMcpTrackingSyncAdapter().sync_daily_tracking({}, "runtime")
            self.assertIn("Arquivos atualizados:", result)
            self.assertEqual(asyncio.run(invoke_tracking_capability("missing", {})), "Ferramenta desconhecida: missing")

    def test_concurrent_dispatch_preserves_each_entry(self) -> None:
        async def append_entries() -> None:
            await asyncio.gather(*[
                invoke_tracking_capability("append_daily_action_log", {"title": f"Entry-{index}", "details": "Test"})
                for index in range(20)
            ])

        with patch.dict(os.environ, {"KWIKLEDGERS_ROOT_DIR": str(self.root)}):
            asyncio.run(append_entries())
        content = (self.service.daily_log_dir / "2026-09-29.md").read_text()
        self.assertEqual(content.count("# Daily Action Log"), 1)
        for index in range(20):
            self.assertEqual(content.count(f" - Entry-{index}\n"), 1)

    def test_multiprocess_dispatch_preserves_twenty_entries(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        code = (
            "import sys\n"
            "from datetime import datetime\n"
            "from unittest.mock import patch\n"
            "from kwikledgers_fastmcp.domains.local_tracking.services import TrackingService\n"
            "service = TrackingService()\n"
            "assert sys.stdin.readline() == 'start\\n'\n"
            "with patch('kwikledgers_fastmcp.domains.local_tracking.services.datetime') as clock:\n"
            "    clock.utcnow.return_value = datetime(2026, 9, 29, 12, 30)\n"
            "    print(service.invoke('append_daily_action_log', "
            "{'title': 'Process-' + sys.argv[1], 'details': 'Test'}))\n"
        )
        processes = []
        try:
            for index in range(20):
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", code, str(index)], cwd=self.root,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env={**os.environ, "PYTHONPATH": str(source_root), "KWIKLEDGERS_ROOT_DIR": str(self.root)},
                ))
            for process in processes:
                self.assertIsNotNone(process.stdin)
                process.stdin.write("start\n")
                process.stdin.flush()
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertIn("Log atualizado:", stdout)
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                process.communicate()
        content = (self.service.daily_log_dir / "2026-09-29.md").read_text(encoding="utf-8")
        self.assertEqual(content.count("# Daily Action Log"), 1)
        self.assertEqual(content.count("### "), 20)
        for index in range(20):
            self.assertEqual(content.count(f" - Process-{index}\n"), 1)
        self.assertTrue((self.root / "AI_Tracking/Audit/tracking.lock").is_file())

    def test_native_imports_are_transport_and_legacy_free(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src"
        code = (
            "import builtins, sys\n"
            "original = builtins.__import__\n"
            "before = list(sys.path)\n"
            "def guarded(name, *args, **kwargs):\n"
            "    if name.split('.')[0] in {'mcp', 'fastmcp', 'local_tracking', 'windows_calendar', 'utils'} or 'legacy' in name:\n"
            "        raise AssertionError(name)\n"
            "    return original(name, *args, **kwargs)\n"
            "builtins.__import__ = guarded\n"
            "from kwikledgers_fastmcp.domains.local_tracking.services import invoke_tracking_capability\n"
            "from kwikledgers_fastmcp.domains.windows.services import invoke_windows_capability\n"
            "assert before == sys.path\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=self.root, capture_output=True, text=True, timeout=10,
            env={**os.environ, "PYTHONPATH": str(source_root)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

