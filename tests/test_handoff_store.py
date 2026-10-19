import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from kwikledgers_fastmcp.domains.local_tracking.handoffs import HandoffStore
from kwikledgers_fastmcp.domains.local_tracking.request_metrics import RequestMetricsService


class HandoffStoreTests(unittest.TestCase):
    def test_atomic_updates_preserve_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = HandoffStore(Path(temporary))
            handoff_id = str(uuid4())
            store.create({"handoff_id": handoff_id, "count": 0})

            def increment(_):
                store.update(handoff_id, lambda current: {**current, "count": current["count"] + 1})

            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(increment, range(16)))
            record = store.read(handoff_id)
            self.assertEqual(record["count"], 16)
            self.assertEqual(record["revision"], 17)
            with self.assertRaises(ValueError):
                store.create({"handoff_id": handoff_id})
            with self.assertRaises(ValueError):
                store.read("../other")

    def test_explicit_request_id_rejects_replay_and_invalid_identifiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            metrics = RequestMetricsService(tracking_root=Path(temporary))
            metrics.start_request("session", request_id="explicit-request")
            with self.assertRaises(ValueError):
                metrics.start_request("other-session", request_id="explicit-request")
            with self.assertRaises(ValueError):
                metrics.start_request("session", request_id="")
            self.assertEqual(metrics.current_request("session")["request_id"], "explicit-request")
