import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kwikledgers_fastmcp.domains.local_tracking.services import TrackingService


class TrackingRootTests(unittest.TestCase):
    def test_custom_root_is_shared_by_snapshot_signals_and_locks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracking = root / "custom"
            with patch.dict(os.environ, {
                "KWIKLEDGERS_ROOT_DIR": str(root),
                "KWIKLEDGERS_AI_TRACKING_ROOT": str(tracking),
            }):
                service = TrackingService()
                service.update_task_control('{"items":[]}')
                with service.locked():
                    self.assertTrue((tracking / "Audit" / "tracking.lock").is_file())
                self.assertTrue((tracking / "Task_Control" / "current-sprint.json").is_file())
                self.assertEqual(TrackingService(root).tracking_dir, root / "AI_Tracking")
