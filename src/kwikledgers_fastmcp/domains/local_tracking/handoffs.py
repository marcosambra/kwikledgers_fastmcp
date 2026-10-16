"""Atomic local storage; lifecycle decisions belong to LangGraph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from .services import TrackingService


class HandoffStore:
    def __init__(self, tracking_root: Path) -> None:
        self.tracking = TrackingService(tracking_root=tracking_root)
        self.directory = self.tracking.tracking_dir / "Task_Control" / "handoffs"

    def path(self, handoff_id: str) -> Path:
        if str(UUID(handoff_id)) != handoff_id:
            raise ValueError("Identificador de handoff inválido.")
        return self.directory / f"{handoff_id}.json"

    def _read(self, handoff_id: str) -> dict[str, Any] | None:
        target = self.path(handoff_id)
        if not target.exists():
            return None
        record = json.loads(target.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or record.get("handoff_id") != handoff_id or type(record.get("revision")) is not int):
            raise ValueError("Registro de handoff inválido.")
        return record

    def read(self, handoff_id: str) -> dict[str, Any] | None:
        with self.tracking.locked():
            return self._read(handoff_id)

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.tracking.locked():
            target = self.path(record["handoff_id"])
            if target.exists():
                raise ValueError("O handoff já existe.")
            self.directory.mkdir(parents=True, exist_ok=True)
            stored = {**record, "schema_version": 1, "revision": 1}
            self.tracking._atomic_write(target, json.dumps(stored, ensure_ascii=False))
            return stored

    def update(
        self, handoff_id: str, transform: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        with self.tracking.locked():
            current = self._read(handoff_id)
            if current is None:
                raise ValueError("Handoff não encontrado.")
            updated = transform(dict(current))
            if updated == current:
                return current, False
            updated = {**updated, "revision": current["revision"] + 1}
            self.tracking._atomic_write(self.path(handoff_id), json.dumps(updated, ensure_ascii=False))
            return updated, True
