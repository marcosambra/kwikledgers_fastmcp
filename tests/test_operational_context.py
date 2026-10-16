import asyncio
import unittest

from kwikledgers_fastmcp.shared.operational_context import (
    METADATA_KEY, OperationalContext, bind_operational_context, current_operational_context,
)


class OperationalContextTests(unittest.TestCase):
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
