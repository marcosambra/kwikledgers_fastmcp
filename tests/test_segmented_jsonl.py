from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from kwikledgers_fastmcp.domains.local_tracking.request_metrics import RequestMetricsService
from kwikledgers_fastmcp.domains.local_tracking.segmented_jsonl import SegmentedJsonl


def _line(index: int, at: str | None = None, pad: int = 0) -> bytes:
    at = at or datetime.now(timezone.utc).isoformat()
    return (json.dumps({"n": index, "at": at, "pad": "x" * pad}) + "\n").encode("utf-8")


class SegmentedJsonlTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.active = self.dir / "events.jsonl"

    def numbers(self, store: SegmentedJsonl) -> list[int]:
        return [json.loads(line)["n"] for path in store.paths()
                for line in path.read_bytes().splitlines()]

    def test_rolls_into_numbered_parts_under_the_cap_without_losing_lines(self) -> None:
        store = SegmentedJsonl(self.active, max_bytes=1000)
        store.append(_line(index, pad=100) for index in range(40))
        self.assertEqual(self.numbers(store), list(range(40)))
        parts = store.archived()
        self.assertGreater(len(parts), 2)
        self.assertEqual([path.name for path in parts[:2]], ["events.part0001.jsonl", "events.part0002.jsonl"])
        for path in store.paths():
            self.assertLessEqual(path.stat().st_size, 1000)
            self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_oversized_active_file_is_split_once_between_lines(self) -> None:
        self.active.write_bytes(b"".join(_line(index, pad=100) for index in range(100)))
        store = SegmentedJsonl(self.active, max_bytes=2000)
        store.append([_line(100)])
        self.assertEqual(self.numbers(store), list(range(101)))
        for path in store.paths():
            self.assertLessEqual(path.stat().st_size, 2000)
        self.assertEqual(json.loads(self.active.read_bytes())["n"], 100)

    def test_parts_older_than_retention_are_deleted_and_recent_kept(self) -> None:
        store = SegmentedJsonl(self.active, max_bytes=500)
        store.append(_line(index, pad=100) for index in range(10))
        old, recent = store.archived()[0], store.archived()[-1]
        stale = time.time() - 15 * 86400
        os.utime(old, (stale, stale))
        store.append([_line(99)])
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_split_parts_are_dated_by_their_last_event(self) -> None:
        self.active.write_bytes(
            b"".join(_line(index, at="2020-01-01T00:00:00Z", pad=100) for index in range(10))
            + b"".join(_line(index, pad=100) for index in range(10, 14)))
        store = SegmentedJsonl(self.active, max_bytes=1000)
        store.append([_line(99)])
        self.assertEqual(self.numbers(store)[-2:], [13, 99])
        self.assertNotIn(0, self.numbers(store))

    def test_unterminated_record_is_refused_when_requested(self) -> None:
        self.active.write_bytes(b'{"n": 1')
        store = SegmentedJsonl(self.active, max_bytes=1000)
        with self.assertRaisesRegex(ValueError, "incompleto"):
            store.append([_line(2)], partial_error="Ledger incompleto")
        self.assertEqual(self.active.read_bytes(), b'{"n": 1')

    def test_request_ledger_reads_span_segments_in_order(self) -> None:
        service = RequestMetricsService(self.dir)
        with patch("kwikledgers_fastmcp.domains.local_tracking.segmented_jsonl.MAX_SEGMENT_BYTES", 2000):
            ids = [service.append_event(
                request_id=f"request-{index}", source="agent", kind="step",
                label="x" * 150, status="completed")["request_id"] for index in range(30)]
        self.assertGreater(len(SegmentedJsonl(service.ledger_path).archived()), 1)
        self.assertEqual([event["request_id"] for event in service.read_events(limit=30)], ids)
        self.assertEqual([event["request_id"] for event in service.read_events(limit=3)], ids[-3:])


if __name__ == "__main__":
    unittest.main()
