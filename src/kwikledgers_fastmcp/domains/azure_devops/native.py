from __future__ import annotations

import asyncio
import inspect
import json
import os
from base64 import b64encode
from typing import Any, Callable

from . import services
from .configuration import load_configuration
from .primitives import EXPECTED_REVISIONS, validate_revision
from .read_services import read_active_user


CAPABILITIES: dict[str, tuple[str, Callable[..., str]]] = {
    "get_active_user": ("read", read_active_user),
    "get_my_work_items": ("read", services._get_my_work_items),
    "get_my_blocked_items": ("read", services._get_my_blocked_items),
    "get_my_daily_summary": ("read", services._get_my_daily_summary),
    "get_user_stories": ("read", services._get_user_stories),
    "get_sprint_stories": ("read", services._get_sprint_stories),
    "get_sprint_stories_detailed": ("read", services._get_sprint_stories_detailed),
    "get_story_details": ("read", services._get_story_details),
    "get_sprint_work_report": ("read", services._get_sprint_work_report),
    "get_open_prs": ("read", services._get_open_prs),
    "preview_story_branch_association": ("preview", services._preview_story_branch_association),
    "preview_story_pull_request": ("preview", services._preview_story_pull_request),
    "associate_story_branch": ("execute", services._associate_story_branch),
    "create_story_pull_request": ("execute", services._create_story_pull_request),
    "create_work_item": ("execute", services._create_work_item),
    "find_similar_technical_debts": ("read", services._find_similar_technical_debts),
    "save_technical_debt_preview_cache": ("execute", services._save_technical_debt_preview_cache),
    "update_work_item_content": ("execute", services._update_work_item_content),
    "update_work_item_effort": ("execute", services._update_work_item_effort),
    "move_work_item_to_next_sprint": ("execute", services._move_work_item_to_next_sprint),
    "update_story_status": ("execute", services._update_story_status),
    "add_story_comment": ("execute", services._add_story_comment),
}

REVISION_REQUIRED_CAPABILITIES = {
    "update_work_item_content", "update_work_item_effort", "move_work_item_to_next_sprint",
    "update_story_status", "add_story_comment",
}
REVISION_MAP_CAPABILITIES = {"associate_story_branch", "create_story_pull_request"}


class AzureCapabilityError(RuntimeError):
    """An Azure SDK failure with credential material removed."""

    def __init__(self, message: str, *, capability: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.capability = capability
        self.status_code = status_code


def _safe_error(error: Exception) -> str:
    message = str(error)
    secret = os.environ.get("AZURE_PAT", "")
    if secret:
        for value in (b64encode(f":{secret}".encode()).decode(), secret):
            message = message.replace(value, "[REDACTED]")
    return message


def _invoke(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name not in CAPABILITIES:
        raise ValueError(f"Unknown Azure capability: {name}")
    operation, handler = CAPABILITIES[name]
    request = dict(arguments)
    revisions = request.pop("expected_revisions", None)
    revision = request.pop("expected_revision", None)
    expected: dict[int, int] = {}
    if revisions is not None:
        if not isinstance(revisions, dict):
            raise ValueError("expected_revisions must map work item IDs to revisions.")
        for work_item_id, value in revisions.items():
            expected[int(work_item_id)] = validate_revision(value)
    if revision is not None:
        work_item_id = request.get("work_item_id", request.get("story_id"))
        if work_item_id is None:
            raise ValueError("expected_revision requires a work_item_id or story_id.")
        normalized_id = int(work_item_id)
        validated_revision = validate_revision(revision)
        if normalized_id in expected and expected[normalized_id] != validated_revision:
            raise ValueError("Conflicting revision guards for the target work item.")
        expected[normalized_id] = validated_revision
    if name in REVISION_REQUIRED_CAPABILITIES:
        target_id = request.get("work_item_id", request.get("story_id"))
        if target_id is None or int(target_id) not in expected:
            raise ValueError("expected_revision is required for an existing work item mutation.")
    if name in REVISION_MAP_CAPABILITIES and not expected:
        raise ValueError("expected_revisions is required for every branch/PR target.")
    if expected and (operation != "execute" or name in {"create_work_item", "save_technical_debt_preview_cache"}):
        raise ValueError("Revision guards apply only to existing work item mutations.")
    inspect.signature(handler).bind(**request)
    load_configuration()
    token = EXPECTED_REVISIONS.set(expected)
    try:
        result = handler(**request)
        try:
            data: Any = json.loads(result)
        except json.JSONDecodeError:
            data = {"message": result}
        return {"capability": name, "domain": "azure_devops", "operation": operation, "data": data}
    except Exception as error:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None) or getattr(error, "status_code", None)
        raise AzureCapabilityError(
            _safe_error(error), capability=name,
            status_code=status_code if isinstance(status_code, int) else None,
        ) from None
    finally:
        EXPECTED_REVISIONS.reset(token)


async def invoke_azure_capability(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_invoke, name, dict(arguments or {}))