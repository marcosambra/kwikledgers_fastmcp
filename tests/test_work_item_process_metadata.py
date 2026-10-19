import asyncio
import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from kwikledgers_fastmcp.domains.azure_devops.native import CAPABILITIES, invoke_azure_capability
from kwikledgers_fastmcp.domains.azure_devops.services import WORK_ITEM_TYPE_CACHE
from kwikledgers_fastmcp.shared.capability_gateway import AZURE_CAPABILITIES


class ProcessMetadataTests(TestCase):
    def setUp(self):
        self.wit = Mock()
        self.wit.get_work_item_types.return_value = [
            SimpleNamespace(name=name) for name in ("User Story", "Task", "Bug")]
        self.wit.get_fields.return_value = [
            SimpleNamespace(reference_name="System.Title", read_only=False)]
        self.field = SimpleNamespace(reference_name="System.Title", always_required=True,
                                     allowed_values=[], default_value=None)
        self.wit.get_work_item_type_fields_with_references.return_value = [self.field]
        client = Mock()
        client.clients.get_work_item_tracking_client.return_value = self.wit
        for target, kwargs in (
            ("kwikledgers_fastmcp.domains.azure_devops.native.load_configuration", {}),
            ("kwikledgers_fastmcp.domains.azure_devops.process_metadata.load_configuration", {}),
            ("kwikledgers_fastmcp.domains.azure_devops.services.load_configuration", {}),
            ("kwikledgers_fastmcp.domains.azure_devops.process_metadata.get_client", {"return_value": client}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {"AZURE_PROJECT": "KL", "AZURE_PROJECT_AREA": "Squad"})
        environment.start()
        self.addCleanup(environment.stop)
        cache = patch.dict(WORK_ITEM_TYPE_CACHE, {}, clear=True)
        cache.start()
        self.addCleanup(cache.stop)

    def invoke(self):
        return asyncio.run(invoke_azure_capability("get_work_item_process_metadata"))

    def test_native_capability_is_read_only_and_preserves_real_constraints(self):
        self.field.allowed_values = ["Approved title"]
        self.field.default_value = "Approved title"
        result = self.invoke()
        self.assertEqual(result["operation"], "read")
        self.assertEqual(result["data"]["project"], "KL")
        self.assertEqual(result["data"]["area"], "Squad")
        self.assertEqual([item["name"] for item in result["data"]["work_item_types"]],
                         ["User Story", "Task", "Bug"])
        self.assertEqual(result["data"]["work_item_types"][1]["fields"], [{
            "reference_name": "System.Title", "read_only": False, "always_required": True,
            "allowed_values": ["Approved title"], "default_value": "Approved title",
        }])
        self.wit.get_work_item_type_fields_with_references.assert_any_call("KL", "Task", expand="All")
        self.assertEqual({call[0] for call in self.wit.method_calls},
                         {"get_work_item_types", "get_fields", "get_work_item_type_fields_with_references"})
        self.assertEqual(CAPABILITIES["get_work_item_process_metadata"][0], "read")
        self.assertEqual(next(item.operation for item in AZURE_CAPABILITIES
                              if item.name == "get_work_item_process_metadata"), "read")

    def test_missing_environment_is_not_defaulted(self):
        for name in ("AZURE_PROJECT", "AZURE_PROJECT_AREA"):
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                with self.assertRaisesRegex(RuntimeError, "requires AZURE_PROJECT"):
                    self.invoke()
        self.wit.get_fields.assert_not_called()

    def test_unavailable_metadata_fails_without_supported_field_fallback(self):
        self.wit.get_work_item_type_fields_with_references.side_effect = RuntimeError("Metadata unavailable")
        with self.assertRaisesRegex(RuntimeError, "Metadata unavailable"):
            self.invoke()
        self.wit.create_work_item.assert_not_called()

    def test_empty_or_incomplete_metadata_fails(self):
        for change in ({"allowed_values": "invalid"}, {"always_required": None},
                       {"reference_name": None}):
            with self.subTest(change=change):
                self.wit.get_work_item_type_fields_with_references.return_value = [
                    SimpleNamespace(**{**vars(self.field), **change})]
                with self.assertRaisesRegex(RuntimeError, "incomplete constraints"):
                    self.invoke()
        self.wit.get_fields.return_value = []
        with self.assertRaisesRegex(RuntimeError, "no project field definitions"):
            self.invoke()

    def test_virtual_fields_and_absent_enumerations_remain_unknown_not_invented(self):
        self.field.reference_name = "System.IterationLevel7"
        self.field.allowed_values = None
        result = self.invoke()["data"]["work_item_types"][0]["fields"][0]
        self.assertIsNone(result["read_only"])
        self.assertIsNone(result["allowed_values"])
