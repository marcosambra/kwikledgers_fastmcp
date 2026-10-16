from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import NAMESPACE_URL, UUID, uuid5

from kwikledgers_fastmcp.domains.local_tracking.request_metrics import RequestMetricsService
from kwikledgers_fastmcp.domains.local_tracking.services import TrackingService
from kwikledgers_fastmcp.shared.capability_gateway import (
    CAPABILITY_CATALOG, CapabilityDefinition, CapabilityMetricsError, invoke_capability,
)
from kwikledgers_fastmcp.shared.execution_capability import ExecutionCapabilityError


def _concurrent_starts(root: str, session: str) -> None:
    service = RequestMetricsService(Path(root))
    for _ in range(10):
        service.start_request(session)


def _concurrent_replay(root: str, event_id: str) -> None:
    service = RequestMetricsService(Path(root))
    for _ in range(10):
        service.append_event(
            request_id="graph-1", source="langgraph", kind="step", label="node",
            status="completed", event_id=event_id,
        )


class RequestMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.service = RequestMetricsService(self.root)

    def append(self, **overrides):
        event = {
            "request_id": "request-1", "source": "agent", "kind": "step",
            "label": "Operação", "status": "completed",
        }
        return self.service.append_event(**{**event, **overrides})

    def test_schema_single_source_and_unknown_duration(self) -> None:
        event = self.append()
        self.assertEqual(set(event), {
            "schema_version", "event_id", "request_id", "session_id", "source",
            "kind", "at", "label", "status", "details",
        })
        UUID(event["event_id"])
        self.assertTrue(event["at"].endswith("Z"))
        self.assertEqual(event["schema_version"], 1)
        self.assertIsNone(event.get("duration_ms"))
        self.assertEqual(self.service.read_events(), [event])
        self.assertEqual(self.service.ledger_path, self.root / "AI_Tracking/Metrics/request-events.jsonl")
        self.assertFalse((self.root / "AI_Tracking/Task_Control/tracking-changed.json").exists())

    def test_custom_tracking_root_and_environment_override(self) -> None:
        custom = self.root / "arbitrary-name"
        service = RequestMetricsService(self.root, tracking_root=custom)
        service.start_request("session")
        self.assertTrue((custom / "Metrics/request-events.jsonl").is_file())
        self.assertTrue((custom / "Audit/tracking.lock").is_file())
        self.assertFalse((self.root / "AI_Tracking").exists())
        with patch.dict(os.environ, {"KWIKLEDGERS_ROOT_DIR": str(self.root),
                                     "KWIKLEDGERS_AI_TRACKING_ROOT": str(custom)}):
            self.assertIsNotNone(RequestMetricsService().current_request("session"))
            self.assertIsNone(RequestMetricsService(self.root).current_request("session"))
            self.assertIsNone(RequestMetricsService(tracking_root=self.root / "explicit").current_request("session"))

    def test_deterministic_event_id_deduplicates_and_rejects_conflicting_reuse(self) -> None:
        event_id = str(uuid5(NAMESPACE_URL, "graph-1/node-1"))
        first = self.append(event_id=event_id)
        with patch.object(self.service, "_now", return_value=datetime(2030, 1, 1, tzinfo=timezone.utc)):
            replay = self.append(event_id=event_id)
        self.assertEqual(replay, first)
        self.assertEqual(len(self.service.read_events()), 1)
        with self.assertRaisesRegex(ValueError, "conteúdo diferente"):
            self.append(event_id=event_id, label="different")
        for invalid in ("unsafe", 1, ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.append(event_id=invalid)

    def test_concurrent_retries_have_one_durable_event(self) -> None:
        context = multiprocessing.get_context("spawn")
        event_id = str(uuid5(NAMESPACE_URL, "same-graph-event"))
        processes = [
            context.Process(target=_concurrent_replay, args=(str(self.root), event_id))
            for _ in range(3)
        ]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                if process.pid is not None:
                    process.close()
        self.assertEqual(len(self.service.read_events()), 1)

    def test_session_lifecycle_duration_and_idempotent_finish(self) -> None:
        now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        with patch.object(self.service, "_now", return_value=now):
            started = self.service.start_request("session-1")
            other = self.service.start_request("session-2")
        with patch.object(self.service, "_now", return_value=now + timedelta(seconds=2.5)):
            finished = self.service.finish_request("session-1")
        self.assertEqual(finished["request_id"], started["request_id"])
        self.assertEqual(finished["duration_ms"], 2500)
        self.assertEqual(finished["details"]["measurement"], "wall_clock_utc")
        self.assertIsNone(self.service.current_request("session-1"))
        self.assertIsNone(self.service.finish_request("session-1"))
        self.assertEqual(self.service.current_request("session-2"), other)
        self.assertIsNone(RequestMetricsService(self.root).current_request("missing"))

    def test_next_prompt_interrupts_only_same_session(self) -> None:
        first = self.service.start_request("session-1")
        other = self.service.start_request("session-2")
        latest = self.service.start_request("session-1")
        events = self.service.read_events()
        self.assertEqual(events[-2]["request_id"], first["request_id"])
        self.assertEqual(events[-2]["status"], "interrupted")
        self.assertNotEqual(first["request_id"], latest["request_id"])
        self.assertEqual(self.service.current_request("session-1"), latest)
        self.assertEqual(self.service.current_request("session-2"), other)

    def test_independent_graph_lifecycle_needs_no_fake_session(self) -> None:
        started = self.append(request_id="graph-123", source="langgraph", kind="request_started", status="running")
        finished = self.append(request_id="graph-123", source="langgraph", kind="request_finished",
                               duration_ms=1.5, details={"observed_at": started["at"], "measurement": "wall_interval"})
        self.assertEqual(finished["session_id"], "")
        self.assertEqual(finished["duration_ms"], 1.5)
        self.assertEqual(len(self.service.read_events()), 2)

    def test_clock_rollback_is_unknown_not_fake_zero(self) -> None:
        now = datetime(2026, 10, 16, tzinfo=timezone.utc)
        with patch.object(self.service, "_now", return_value=now):
            self.service.start_request("session")
        with patch.object(self.service, "_now", return_value=now - timedelta(seconds=1)):
            event = self.service.finish_request("session")
        self.assertIsNone(event.get("duration_ms"))
        self.assertEqual(event["details"]["reason"], "wall_clock_moved_backwards")

    def test_invalid_events_never_append(self) -> None:
        cases = [
            {"request_id": "../secret"}, {"request_id": "x\nsecret"}, {"request_id": ""},
            {"session_id": "x" * 129}, {"source": "other"}, {"kind": "other"},
            {"status": "other"}, {"source": []}, {"label": None},
            {"kind": "request_started", "status": "completed"},
            {"kind": "request_finished", "status": "running"},
            *({"duration_ms": value} for value in (-1, float("nan"), float("inf"), True, "1")),
            {"details": []}, {"details": {"node": {"token": "secret"}}},
            {"details": {"error": "false"}},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.append(**case)
        self.assertFalse(self.service.ledger_path.exists())

    def test_invalid_start_or_finish_preserves_open_request(self) -> None:
        started = self.service.start_request("session")
        with self.assertRaises(ValueError):
            self.service.start_request("session", label=None)
        for status in ("running", "other", None, []):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.service.finish_request("session", status=status)
        self.assertEqual(self.service.current_request("session"), started)
        self.assertEqual(len(self.service.read_events()), 1)

    def test_details_allowlist_redaction_and_limits(self) -> None:
        event = self.append(
            label="password=ultrasecret\nBearer credential-value",
            details={
                "args": {"password": "secret"}, "prompt": "user prompt", "result": "raw response",
                "node": "safe_node", "evidence": "token=private-key",
                "reason": "https://user:password@example.com/path?api_key=secret",
                "tool_name": "x" * 1000, "error": True,
                "parent_request_id": "graph-12345678-1234-1234-1234-123456789012",
            },
        )
        serialized = json.dumps(event)
        for secret in ("ultrasecret", "credential-value", "private-key", "user prompt", "raw response", "example.com"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(set(event["details"]), {
            "node", "evidence", "reason", "tool_name", "error", "parent_request_id",
        })
        self.assertEqual(event["details"]["node"], "safe_node")
        self.assertEqual(len(event["details"]["tool_name"]), 160)
        self.assertEqual(event["details"]["parent_request_id"], "graph-12345678-1234-1234-1234-123456789012")
        self.assertEqual(self.append(label="a " * 1000)["label"], ("a " * 1000)[:160])

    def test_bounded_tail_and_invalid_limits(self) -> None:
        events = [self.append(label=f"step-{i}") for i in range(8)]
        self.assertEqual(self.service.read_events(3), events[-3:])
        for limit in (0, 1001, True, 1.5, "2"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.service.read_events(limit)
        # Tail reads do not parse old lines outside the requested window.
        data = self.service.ledger_path.read_bytes()
        self.service.ledger_path.write_bytes(b"invalid old line\n" + data)
        self.assertEqual(self.service.read_events(2), events[-2:])
        with self.assertRaises(ValueError):
            self.service.read_events(100)

    def test_reverse_reader_crosses_chunks_and_utf8_boundaries(self) -> None:
        events = [self.append(label="ação " * 30) for _ in range(200)]
        self.assertGreater(self.service.ledger_path.stat().st_size, 65536)
        self.assertEqual(self.service.read_events(200), events)

    def test_partial_ledger_and_io_errors_are_not_hidden(self) -> None:
        self.append()
        with self.service.ledger_path.open("ab") as handle:
            handle.write(b'{"partial"')
        before = self.service.ledger_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "incompleto"):
            self.service.read_events()
        with self.assertRaisesRegex(ValueError, "incompleto"):
            self.append()
        self.assertEqual(self.service.ledger_path.read_bytes(), before)
        with patch("kwikledgers_fastmcp.domains.local_tracking.request_metrics.os.fsync",
                   side_effect=OSError("disk unavailable")), self.assertRaises(OSError):
            RequestMetricsService(self.root / "other").start_request("session")

    def test_reuses_existing_tracking_lock(self) -> None:
        with patch.object(TrackingService, "locked", wraps=self.service._tracking.locked) as locked:
            self.append()
            self.service.read_events()
        self.assertEqual(locked.call_count, 2)
        self.assertTrue((self.root / "AI_Tracking/Audit/tracking.lock").is_file())

    def test_concurrent_processes_serialize_same_session_and_preserve_distinct_sessions(self) -> None:
        context = multiprocessing.get_context("spawn")
        for sessions in (["shared"] * 4, [f"session-{i}" for i in range(4)]):
            root = self.root / sessions[0]
            processes = [context.Process(target=_concurrent_starts, args=(str(root), session)) for session in sessions]
            try:
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(timeout=30)
                    self.assertEqual(process.exitcode, 0)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                    if process.pid is not None:
                        process.close()
            service = RequestMetricsService(root)
            events = service.read_events(1000)
            self.assertEqual(len(events), 80 - len(set(sessions)))
            self.assertEqual(len({event["event_id"] for event in events}), len(events))
            for session in set(sessions):
                self.assertIsNotNone(service.current_request(session))
                service.finish_request(session)
                self.assertIsNone(service.current_request(session))


class RequestMetricsGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {"KWIKLEDGERS_ROOT_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("KWIKLEDGERS_REQUEST_ID", None)
        self.service = RequestMetricsService(self.root)
        self.definition = CapabilityDefinition("get_active_user", "azure_devops", "read", "test")
        self.dispatch = AsyncMock(return_value={"safe": "password=not-for-ledger"})
        dispatcher = patch("kwikledgers_fastmcp.shared.capability_gateway.invoke_azure_capability", self.dispatch)
        dispatcher.start()
        self.addCleanup(dispatcher.stop)

    def invoke(self, definition=None, arguments=None):
        return asyncio.run(invoke_capability(definition or self.definition, arguments))

    def test_gateway_measures_only_safe_metadata_and_no_session_guess(self) -> None:
        self.service.start_request("unrelated-session")
        with patch("kwikledgers_fastmcp.shared.capability_gateway.perf_counter", side_effect=[10, 10.25, 10.3]):
            result = self.invoke(arguments={"arbitrary": "token=do-not-store"})
        events = self.service.read_events()
        event = events[-2]
        UUID(event["request_id"])
        self.assertEqual(event["session_id"], "")
        self.assertEqual(event["kind"], "step")
        self.assertEqual(event["duration_ms"], 250)
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["details"], {
            "capability": "get_active_user", "domain": "azure_devops",
            "operation": "read", "outcome": "completed",
        })
        self.assertEqual([item["kind"] for item in events[-3:]], ["request_started", "step", "request_finished"])
        self.assertEqual(len({item["request_id"] for item in events[-3:]}), 1)
        self.assertAlmostEqual(events[-1]["duration_ms"], 300)
        self.assertNotIn("not-for-ledger", self.service.ledger_path.read_text())
        self.assertNotIn("do-not-store", self.service.ledger_path.read_text())
        self.assertEqual(result["data"], {"safe": "password=not-for-ledger"})

    def test_explicit_context_and_rejection_of_unsafe_context(self) -> None:
        with patch.dict(os.environ, {"KWIKLEDGERS_REQUEST_ID": "graph-123"}):
            self.invoke()
        self.assertEqual(self.service.read_events()[0]["request_id"], "graph-123")
        self.dispatch.reset_mock()
        with patch.dict(os.environ, {"KWIKLEDGERS_REQUEST_ID": "../unsafe"}), self.assertRaises(ValueError):
            self.invoke()
        self.dispatch.assert_not_awaited()

    def test_workflow_and_token_gates_stay_closed(self) -> None:
        for key in ("request_id", "approved", "run_id", "pending_approval"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.invoke(arguments={key: "forbidden"})
        execute = CapabilityDefinition("create_work_item", "azure_devops", "execute", "test")
        with self.assertRaises(ExecutionCapabilityError):
            self.invoke(execute, {"title": "do not execute"})
        self.dispatch.assert_not_awaited()
        self.assertTrue(all(event["status"] == "blocked"
                            for event in self.service.read_events() if event["kind"] != "request_started"))

    def test_handler_error_is_recorded_without_error_message(self) -> None:
        error = RuntimeError("password=private")
        self.dispatch.side_effect = error
        with self.assertRaises(RuntimeError) as raised:
            self.invoke()
        self.assertIs(raised.exception, error)
        event = self.service.read_events()[-1]
        self.assertEqual(event["status"], "failed")
        self.assertNotIn("private", self.service.ledger_path.read_text())

    def test_cancelled_handler_is_interrupted(self) -> None:
        self.dispatch.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.invoke()
        self.assertEqual(self.service.read_events()[-1]["status"], "interrupted")

    def test_post_execute_ledger_failure_exposes_committed_result_and_no_retry(self) -> None:
        execute = CapabilityDefinition("create_work_item", "azure_devops", "execute", "test")
        with patch.dict(os.environ, {"KWIKLEDGERS_REQUEST_ID": "graph-1"}), patch(
            "kwikledgers_fastmcp.shared.capability_gateway.verify_execution_capability",
        ) as verify, patch.object(
            RequestMetricsService, "append_event", side_effect=OSError("password=private"),
        ), self.assertRaises(CapabilityMetricsError) as raised:
            self.invoke(execute, {"execution_capability": "opaque", "title": "test"})
        verify.assert_called_once_with("opaque", capability_name="create_work_item", arguments={"title": "test"})
        self.dispatch.assert_awaited_once()
        self.assertTrue(raised.exception.operation_executed)
        self.assertTrue(raised.exception.operation_dispatched)
        self.assertEqual(raised.exception.result["data"], self.dispatch.return_value)
        self.assertIn("JÁ RETORNOU", str(raised.exception))
        self.assertIn("Não repita", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def test_ledger_failure_does_not_mask_failed_or_blocked_outcome(self) -> None:
        for dispatched in (True, False):
            self.dispatch.reset_mock()
            self.dispatch.side_effect = RuntimeError("original")
            with patch.dict(os.environ, {"KWIKLEDGERS_REQUEST_ID": "graph-1"}), patch.object(
                RequestMetricsService, "append_event", side_effect=OSError("disk"),
            ), self.assertRaises(
                CapabilityMetricsError,
            ) as raised:
                self.invoke(arguments={} if dispatched else {"request_id": "forbidden"})
            self.assertEqual(raised.exception.operation_dispatched, dispatched)
            self.assertFalse(raised.exception.operation_executed)
            self.assertIsInstance(raised.exception.operation_error, RuntimeError if dispatched else ValueError)

    def test_failure_to_start_standalone_request_blocks_dispatch(self) -> None:
        with patch.object(RequestMetricsService, "append_event", side_effect=OSError("disk")), self.assertRaises(
            CapabilityMetricsError,
        ) as raised:
            self.invoke()
        self.dispatch.assert_not_awaited()
        self.assertFalse(raised.exception.operation_dispatched)
        self.assertIn("NÃO foi enviada", str(raised.exception))

    def test_metrics_capabilities_are_not_recursively_instrumented_and_record_is_guarded(self) -> None:
        definitions = {definition.name: definition for definition in CAPABILITY_CATALOG}
        read, record = definitions["read_request_metrics"], definitions["record_request_event"]
        self.assertEqual(record.operation, "execute")
        self.assertEqual(self.invoke(read)["data"], {"events": []})
        self.assertFalse(self.service.ledger_path.exists())
        event = {
            "request_id": "graph-1", "source": "langgraph", "kind": "step",
            "label": "test", "status": "completed",
        }
        with self.assertRaises(ExecutionCapabilityError):
            self.invoke(record, {"event": event})
        self.assertFalse(self.service.ledger_path.exists())
        with patch("kwikledgers_fastmcp.shared.capability_gateway.verify_execution_capability") as verify:
            result = self.invoke(record, {"event": event, "execution_capability": "opaque"})
        verify.assert_called_once_with("opaque", capability_name="record_request_event", arguments={"event": event})
        self.assertEqual(len(self.service.read_events()), 1)
        self.assertEqual(result["data"]["request_id"], "graph-1")
        self.assertEqual(len(self.invoke(read, {"limit": 1})["data"]["events"]), 1)
        with self.assertRaises(ValueError):
            self.invoke(record, {"request_id": "still-forbidden"})

    def test_metrics_read_capability_uses_configured_tracking_directory(self) -> None:
        custom = self.root / "custom-tracking"
        service = RequestMetricsService(self.root, tracking_root=custom)
        event = service.start_request("session")
        read = next(item for item in CAPABILITY_CATALOG if item.name == "read_request_metrics")
        with patch.dict(os.environ, {"KWIKLEDGERS_AI_TRACKING_ROOT": str(custom)}):
            self.assertEqual(self.invoke(read)["data"]["events"], [event])
        self.assertFalse(self.service.ledger_path.exists())


if __name__ == "__main__":
    unittest.main()
