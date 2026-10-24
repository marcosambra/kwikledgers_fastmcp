"""Append-only operational timing; never CPU/active-work accounting."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID, uuid4

from .segmented_jsonl import SegmentedJsonl
from .services import TrackingService

SOURCES = frozenset({"agent", "fastmcp", "langgraph"})
KINDS = frozenset({"request_started", "step", "request_finished"})
STATUSES = frozenset({"running", "completed", "failed", "blocked", "interrupted"})
DETAIL_FIELDS = frozenset({
    "capability", "domain", "operation", "outcome", "flow_name", "node",
    "correlation_id", "run_id", "parent_request_id", "observed_at", "measurement",
    "tool_name", "tool_call_id", "reason", "evidence", "error_type", "scope", "error",
    "workspace_id", "session_id", "span_id", "parent_span_id",
})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SENSITIVE = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:password|passwd|secret|token|api[_-]?key|"
    r"authorization|cookie|connection[_-]?string|credential|prompt|arguments?|"
    r"results?)\s*[:=]\s*\S+|https?://[^\s]+|"
    r"(?:gh[pousr]_|github_pat_|sk-|AZDO)[A-Za-z0-9_-]{12,}|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
MAX_LINE_BYTES = 16384


def _identifier(value: Any, field: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} deve ser um identificador seguro de até 128 caracteres.")
    return value


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("Metadados textuais devem ser strings.")
    # Sanitize before truncating so a credential cannot straddle the limit.
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", _SENSITIVE.sub("[redacted]", value)).split())[:limit]


class RequestMetricsService:
    """The JSONL ledger is the only source for request state and elapsed time."""

    def __init__(
        self, root: Path | None = None, *, tracking_root: Path | None = None,
    ) -> None:
        self._tracking = TrackingService(root)
        self.root = self._tracking.root
        configured_tracking_root = os.environ.get("KWIKLEDGERS_AI_TRACKING_ROOT") if root is None else None
        selected_tracking_root = tracking_root if tracking_root is not None else configured_tracking_root
        if selected_tracking_root:
            self._tracking.tracking_dir = Path(selected_tracking_root).expanduser().resolve()
        self.tracking_dir = self._tracking.tracking_dir
        self.ledger_path = self.tracking_dir / "Metrics" / "request-events.jsonl"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _event(
        self, *, request_id: str, session_id: str = "", source: str, kind: str,
        label: str, status: str, duration_ms: float | None = None,
        details: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        _identifier(request_id, "request_id")
        _identifier(session_id, "session_id", optional=True)
        if (not isinstance(source, str) or source not in SOURCES
                or not isinstance(kind, str) or kind not in KINDS
                or not isinstance(status, str) or status not in STATUSES):
            raise ValueError("source, kind ou status de métrica inválido.")
        if kind == "request_started" and status != "running":
            raise ValueError("request_started exige status running.")
        if kind == "request_finished" and status == "running":
            raise ValueError("request_finished exige status terminal.")
        if duration_ms is not None:
            if (isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float))
                    or not math.isfinite(duration_ms) or duration_ms < 0):
                raise ValueError("duration_ms deve ser finito e não negativo, ou None.")
        if details is not None and not isinstance(details, dict):
            raise ValueError("details deve ser um objeto.")
        safe_details: dict[str, Any] = {}
        for key, value in (details or {}).items():
            if key not in DETAIL_FIELDS:
                continue
            if key == "error":
                if not isinstance(value, bool):
                    raise ValueError("details.error deve ser booleano.")
                safe_details[key] = value
            else:
                safe_details[key] = _text(value, 160)
        if event_id is not None:
            if not isinstance(event_id, str):
                raise ValueError("event_id deve ser um UUID.")
            try:
                event_id = str(UUID(event_id))
            except ValueError as error:
                raise ValueError("event_id deve ser um UUID.") from error
        event = {
            "schema_version": 1,
            "event_id": event_id or str(uuid4()),
            "request_id": request_id,
            "session_id": session_id,
            "source": source,
            "kind": kind,
            "at": self._now().isoformat().replace("+00:00", "Z"),
            "label": _text(label, 160),
            "status": status,
            "details": safe_details,
        }
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        return event

    def _append(self, event: dict[str, Any]) -> dict[str, Any]:
        line = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(line) > MAX_LINE_BYTES:
            raise ValueError("Evento excede o limite de tamanho.")
        # Refuse a partial last record rather than silently joining JSON objects.
        SegmentedJsonl(self.ledger_path).append(
            [line], fsync=True,
            partial_error="Ledger de métricas incompleto; reconcilie antes de gravar.",
        )
        return event

    def _reverse_events(self) -> Iterator[dict[str, Any]]:
        """Newest first across every segment, active file first."""
        for path in reversed(SegmentedJsonl(self.ledger_path).paths()):
            yield from self._reverse_file(path)

    def _reverse_file(self, path: Path) -> Iterator[dict[str, Any]]:
        if not path.exists():
            return
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            pending = b""
            first_chunk = True
            while position:
                size = min(position, 65536)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                if first_chunk and not chunk.endswith(b"\n"):
                    raise ValueError("Ledger de métricas incompleto.")
                first_chunk = False
                lines = (chunk + pending).split(b"\n")
                pending = lines.pop(0)
                for line in reversed(lines):
                    if line:
                        yield self._decode(line)
                if len(pending) > MAX_LINE_BYTES:
                    raise ValueError("Linha de métricas excede o limite.")
            if pending:
                yield self._decode(pending)

    @staticmethod
    def _decode(line: bytes) -> dict[str, Any]:
        if len(line) > MAX_LINE_BYTES:
            raise ValueError("Linha de métricas excede o limite.")
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError) as error:
            raise ValueError("Ledger de métricas inválido.") from error
        if (not isinstance(event, dict) or event.get("schema_version") != 1
                or event.get("kind") not in KINDS
                or event.get("status") not in STATUSES):
            raise ValueError("Evento de métricas inválido.")
        return event

    def read_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the last 1..1000 events, oldest first, with bounded memory."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit deve ser inteiro entre 1 e 1000.")
        with self._tracking.locked():
            events = []
            for event in self._reverse_events():
                events.append(event)
                if len(events) == limit:
                    break
            return list(reversed(events))

    def _current(self, session_id: str) -> dict[str, Any] | None:
        closed: set[str] = set()
        for event in self._reverse_events():
            if event.get("session_id") != session_id:
                continue
            if event["kind"] == "request_finished":
                closed.add(event["request_id"])
            elif event["kind"] == "request_started" and event["request_id"] not in closed:
                return event
        return None

    def current_request(self, session_id: str) -> dict[str, Any] | None:
        _identifier(session_id, "session_id")
        with self._tracking.locked():
            return self._current(session_id)

    def _finish(self, started: dict[str, Any], status: str) -> dict[str, Any]:
        now = self._now()
        at = datetime.fromisoformat(started["at"].replace("Z", "+00:00"))
        elapsed_ms = (now - at).total_seconds() * 1000
        # A backwards wall clock cannot provide a meaningful nonnegative duration.
        details = {"measurement": "wall_clock_utc"}
        if elapsed_ms < 0:
            details["reason"] = "wall_clock_moved_backwards"
        return self._append(self._event(
            request_id=started["request_id"], session_id=started["session_id"],
            source="agent", kind="request_finished", label=started["label"],
            status=status, duration_ms=elapsed_ms if elapsed_ms >= 0 else None,
            details=details,
        ))

    def start_request(
        self, session_id: str, label: str = "Solicitação do usuário",
        *, request_id: str | None = None,
    ) -> dict[str, Any]:
        _identifier(session_id, "session_id")
        if request_id is not None:
            _identifier(request_id, "request_id")
        # Validate first; an invalid start must not interrupt the current request.
        event = self._event(
            request_id=request_id or str(uuid4()), session_id=session_id, source="agent",
            kind="request_started", label=label, status="running",
            details={"measurement": "wall_clock_utc"},
        )
        with self._tracking.locked():
            if request_id is not None and any(event["request_id"] == request_id for event in self._reverse_events()):
                raise ValueError("O identificador de solicitação já foi utilizado.")
            current = self._current(session_id)
            if current is not None:
                self._finish(current, "interrupted")
            # The timestamp represents actual persistence order under contention.
            event["at"] = self._now().isoformat().replace("+00:00", "Z")
            return self._append(event)

    def append_event(
        self, *, request_id: str, session_id: str = "", source: str, kind: str,
        label: str, status: str, duration_ms: float | None = None,
        details: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        event = self._event(
            request_id=request_id, session_id=session_id, source=source, kind=kind,
            label=label, status=status, duration_ms=duration_ms, details=details,
            event_id=event_id,
        )
        with self._tracking.locked():
            if event_id is not None:
                for existing in self._reverse_events():
                    if existing["event_id"] == event["event_id"]:
                        if ({key: value for key, value in existing.items() if key != "at"}
                                != {key: value for key, value in event.items() if key != "at"}):
                            raise ValueError("event_id já registrado com conteúdo diferente.")
                        return existing
            return self._append(event)

    def finish_request(
        self, session_id: str, status: str = "completed",
    ) -> dict[str, Any] | None:
        _identifier(session_id, "session_id")
        if not isinstance(status, str) or status not in STATUSES - {"running"}:
            raise ValueError("finish_request exige status terminal.")
        with self._tracking.locked():
            current = self._current(session_id)
            return self._finish(current, status) if current is not None else None
