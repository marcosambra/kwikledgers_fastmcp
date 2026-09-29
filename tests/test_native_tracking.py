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
from unittest.mock import patch

from kwikledgers_fastmcp.domains.local_tracking.services import (
    FastMcpTrackingSyncAdapter, TrackingService, invoke_tracking_capability,
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

    def test_byte_for_byte_legacy_artifact_parity_without_importing_server(self) -> None:
        legacy_path = Path(__file__).resolve().parents[2] / "agent/mcp_servers/local_tracking/server.py"
        if not legacy_path.exists():
            self.skipTest("Legacy source is unavailable outside the workspace")
        source = ast.parse(legacy_path.read_text(encoding="utf-8"))
        functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name.startswith("_")]
        for function in functions:
            function.decorator_list = []
        namespace = {
            "Any": Any, "Path": Path, "json": json, "datetime": self.clock,
            "TASK_CONTROL_DIR": self.service.task_control_dir,
            "DAILY_LOG_DIR": self.service.daily_log_dir,
            "METRICS_DIR": self.service.metrics_dir,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(legacy_path), "exec"), namespace)
        payload = {
            "user_email": "dev@example.com", "project": "Kwik Ledgers",
            "user_identity_context": {"resolved_email": "dev@example.com", "email_source": "env", "env_file_path": "/tmp/.env"},
            "items": [{"id": 1, "title": "Correção", "state": "Active", "work_item_type": "Task", "is_blocked": True}],
            "blocked_items": [{"id": 1, "title": "Correção", "state": "Active"}],
            "counts": {"assigned_items": 1, "blocked_items": 1, "tasks_all_statuses": 2,
                       "assigned_items_by_type": {"Task": 2, "Bug": 1}, "tasks_by_state": {"Closed": 1, "Active": 1}},
            "remaining_story_points": 8, "assigned_story_points": 3, "remaining_work_hours": 4,
            "sprint_context": {"name": "Sprint 20", "path": "KL\\2026\\Sprint 20", "start_date": "2026-09-28", "finish_date": "2026-10-09"},
            "project_progress": {"total_stories": 2, "progress_warning": "Sem estimativa", "blocked_story_points": 3},
            "child_items_summary": {"total_children": 2, "blocked_children": 1, "open_children": 2,
                                    "children_by_type": {"Task": 2}, "children_by_parent": [
                                        {"parent_id": 1, "parent_title": "Pai", "child_count": 2, "open_child_count": 2, "blocked_child_count": 1}]},
            "sprint_stories": [{"id": 1, "title": "História", "state": "Active", "story_points": 3, "assigned_to": "Dev", "is_blocked": True}],
            "open_prs": [{"pull_request_id": 2, "title": "PR", "repository": "repo", "age_days": 2}],
        }

        def exercise(handlers: dict) -> tuple[list, dict]:
            results = [handlers["read_tracking_snapshot"]()]
            results.append(handlers["update_task_control"]('{"items": [{"id": 1, "state": "QA"}]}'))
            results.append(handlers["update_task_control"](json.dumps(payload)))
            results.append(handlers["append_daily_action_log"]("Title", "Details", "agent"))
            results.append(handlers["append_daily_action_log"]("Second", "Action", "developer"))
            results.append(handlers["record_ai_metrics"](entry_title="AI", summary="A | B", remaining_story_points=None, blocked_items=None, actions_taken=["Action"]))
            results.append(handlers["record_ai_metrics"](entry_title="AI", summary="Summary", remaining_story_points=0, blocked_items=0, actions_taken=[]))
            results.append(handlers["sync_daily_tracking"](json.dumps(payload), ["Action"], None, None, None, None, "agent"))
            results.append(handlers["sync_daily_tracking"]("{}", [], "Custom", "Details", "Metrics", "Summary", "developer"))
            results.append(handlers["read_tracking_snapshot"]())
            return results, {str(path.relative_to(self.root)): path.read_bytes()
                             for path in self.root.rglob("*") if path.is_file()}

        names = ("read_tracking_snapshot", "update_task_control", "append_daily_action_log", "record_ai_metrics", "sync_daily_tracking")
        native_result = exercise({name: getattr(self.service, name) for name in names})
        shutil.rmtree(self.service.tracking_dir)
        legacy_result = exercise({name: namespace[f"_{name}"] for name in names})
        self.assertEqual(native_result, legacy_result)


if __name__ == "__main__":
    unittest.main()