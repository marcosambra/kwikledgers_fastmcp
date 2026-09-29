from __future__ import annotations

from contextvars import ContextVar
from typing import Any


EXPECTED_REVISIONS: ContextVar[dict[int, int]] = ContextVar("azure_expected_revisions", default={})


def validate_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_revision must be a positive integer.")
    return value


def work_item_revision(wit: Any, work_item_id: int) -> int:
    item = wit.get_work_item(id=work_item_id)
    return validate_revision(getattr(item, "rev", None))


def update_work_item(
    wit: Any,
    document: list[dict[str, Any]],
    id: int,
    observed_revision: int | None = None,
) -> Any:
    revision = EXPECTED_REVISIONS.get().get(int(id), observed_revision)
    revision = validate_revision(revision)
    guarded_document = list(document)
    guarded_document.insert(0, {"op": "test", "path": "/rev", "value": revision})
    result = wit.update_work_item(document=guarded_document, id=id)
    returned_revision = validate_revision(getattr(result, "rev", None))
    if getattr(result, "id", None) != int(id) or returned_revision <= revision:
        raise RuntimeError("Azure did not confirm the updated item and an advanced revision.")
    expected = EXPECTED_REVISIONS.get()
    if int(id) in expected:
        EXPECTED_REVISIONS.set({**expected, int(id): returned_revision})
    return result