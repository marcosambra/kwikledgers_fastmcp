import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kwikledgers_fastmcp.shared.operational_context import (
    METADATA_KEY, OperationalContext, bind_operational_context, current_operational_context,
)


class OperationalContextTests(unittest.TestCase):
    def test_request_without_metadata_cannot_inherit_server_process_identity(self):
        with patch.dict(os.environ, {"KWIKLEDGERS_REQUEST_ID": "stale-server-request"}):
            with bind_operational_context(None):
                self.assertIsNone(current_operational_context())
            self.assertEqual(current_operational_context().request_id, "stale-server-request")

    def test_correlated_execution_still_requires_a_real_capability(self):
        from kwikledgers_fastmcp.shared.capability_gateway import CapabilityDefinition, invoke_capability
        from kwikledgers_fastmcp.shared.execution_capability import ExecutionCapabilityError

        with tempfile.TemporaryDirectory() as temporary, \
             patch.dict(os.environ, {"KWIKLEDGERS_ROOT_DIR": temporary, "KWIKLEDGERS_AI_TRACKING_ROOT": temporary}), \
             bind_operational_context(OperationalContext("request-1", session_id="session-1")):
            with self.assertRaises(ExecutionCapabilityError):
                asyncio.run(invoke_capability(
                    CapabilityDefinition("update_story_status", "azure_devops", "execute", "fixture"),
                    {"story_id": 42, "status": "Closed", "expected_revision": 1},
                ))
            events = [json.loads(line) for line in (Path(temporary) / "Metrics" / "request-events.jsonl").read_text().splitlines()]
            self.assertEqual(events[-1]["status"], "blocked")
            self.assertEqual(events[-1]["session_id"], "session-1")

    def test_round_trip_and_child_spans(self):
        context = OperationalContext("request-1", workspace_id="workspace-1", span_id="root")
        child = context.child()
        self.assertEqual(OperationalContext.from_metadata(child.metadata()), child)
        self.assertEqual(child.request_id, context.request_id)
        self.assertEqual(child.parent_span_id, "root")
        self.assertNotEqual(child.span_id, context.span_id)

    def test_metadata_is_strict_and_never_authorizes(self):
        for value in (
            {"schema_version": 2, "request_id": "r"},
            {"schema_version": True, "request_id": "r"},
            {"schema_version": 1, "request_id": "../secret"},
            {"schema_version": 1, "request_id": "r", "approved": True},
            {"request_id": "r"}, [],
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OperationalContext.from_metadata({METADATA_KEY: value})
        self.assertIsNone(OperationalContext.from_metadata({}))

    def test_concurrent_requests_do_not_share_context(self):
        async def worker(request_id):
            with bind_operational_context(OperationalContext(request_id)):
                await asyncio.sleep(0)
                return current_operational_context().request_id

        async def run():
            return await asyncio.gather(worker("one"), worker("two"))

        self.assertEqual(asyncio.run(run()), ["one", "two"])
