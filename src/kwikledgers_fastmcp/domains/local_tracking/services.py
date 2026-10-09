from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any, Iterator

from .artifacts import build_control_markdown, build_default_log_details

_TRACKING_LOCK = RLock()


def resolve_workspace_root() -> Path:
    explicit_root = os.environ.get("KWIKLEDGERS_ROOT_DIR")
    if explicit_root:
        return Path(explicit_root).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        native_workspace = (
            (parent / "kwikledgers_fastmcp").is_dir()
            and (parent / "kwikledgers_langgraph").is_dir()
        )
        if (parent / ".vscode").is_dir() and ((parent / "AGENTS.md").is_file() or native_workspace):
            return parent
    raise RuntimeError("Defina KWIKLEDGERS_ROOT_DIR para o workspace de tracking.")


class TrackingService:
    """Transport-free writer preserving the local tracking artifact contracts."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else resolve_workspace_root()
        self.tracking_dir = self.root / "AI_Tracking"
        self.task_control_dir = self.tracking_dir / "Task_Control"
        self.daily_log_dir = self.tracking_dir / "Daily_Action_Logs"
        self.metrics_dir = self.tracking_dir / "Metrics"

    def _ensure_directories(self) -> None:
        for directory in (self.task_control_dir, self.daily_log_dir, self.metrics_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _filesystem_lock(self) -> Iterator[None]:
        audit_dir = self.tracking_dir / "Audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        with (audit_dir / "tracking.lock").open("a+b", buffering=0) as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                except OSError as error:
                    raise RuntimeError("Unable to acquire tracking filesystem lock") from error
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _today_stamp() -> str:
        return datetime.utcnow().strftime("%Y-%m-%d")

    @staticmethod
    def _parse_summary(summary: str) -> dict[str, Any]:
        payload = json.loads(summary)
        if not isinstance(payload, dict):
            raise ValueError("Resumo invalido: esperado objeto JSON")
        return payload

    def update_task_control(self, summary: str) -> str:
        self._ensure_directories()
        payload = self._parse_summary(summary)
        json_path = self.task_control_dir / "current-sprint.json"
        md_path = self.task_control_dir / "current-sprint.md"
        previous = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else {}
        previous_items = {
            item.get("id"): item
            for item in (previous.get("items", []) if isinstance(previous, dict) else [])
            if isinstance(item, dict) and item.get("id") is not None
        }
        returned_items = []
        for item in payload.get("items", []):
            if not isinstance(item, dict) or item.get("id") not in previous_items:
                continue
            previous_state = str(previous_items[item["id"]].get("state", "")).strip()
            current_state = str(item.get("state", "")).strip()
            if previous_state and current_state and previous_state != current_state:
                returned_items.append({
                    "id": item["id"], "title": item.get("title"),
                    "previous_state": previous_state, "current_state": current_state,
                })
        payload["returned_items"] = returned_items
        self._atomic_write(json_path, json.dumps(payload, indent=2, ensure_ascii=True) + "\n")
        self._atomic_write(md_path, build_control_markdown(payload, self._today_stamp()))
        return (
            f"Arquivos atualizados: {json_path} e {md_path}. "
            f"Itens devolvidos detectados: {len(returned_items)}"
        )

    def append_daily_action_log(self, title: str, details: str, source: str = "agent") -> str:
        self._ensure_directories()
        log_path = self.daily_log_dir / f"{self._today_stamp()}.md"
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
        if not log_path.exists():
            log_path.write_text(
                "# Daily Action Log\n\n"
                f"Data: {self._today_stamp()}\n"
                "Escopo: atividade operacional do agente e do desenvolvedor\n\n"
                "## Entradas\n\n", encoding="utf-8",
            )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"### {timestamp} - {title}\nFonte: {source}\n\n{details}\n\n")
        return f"Log atualizado: {log_path}"

    def record_ai_metrics(
        self, entry_title: str, summary: str, actions_taken: list[str],
        remaining_story_points: Any = None, blocked_items: Any = None,
    ) -> str:
        self._ensure_directories()
        metrics_path = self.metrics_dir / "ai-usage-log.md"
        sprint_metrics_path = self.metrics_dir / "sprint-metrics.md"
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
        if not metrics_path.exists():
            metrics_path.write_text(
                "# AI Usage Metrics Log\n\n"
                "Registro operacional das interacoes com IA relacionadas ao sprint atual.\n\n",
                encoding="utf-8",
            )
        lines = [f"## {timestamp} - {entry_title}", "", f"Resumo: {summary}"]
        if remaining_story_points is not None:
            lines.append(f"Story points restantes: {remaining_story_points}")
        if blocked_items is not None:
            lines.append(f"Itens bloqueados: {blocked_items}")
        lines.append("Acoes executadas:")
        lines.extend(f"- {action}" for action in actions_taken)
        lines.append("")
        with metrics_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        if not sprint_metrics_path.exists():
            sprint_metrics_path.write_text(
                "# Sprint Metrics Snapshot\n\n"
                "| Data | Story Points Restantes | Itens Bloqueados | Resumo |\n"
                "|---|---:|---:|---|\n", encoding="utf-8",
            )
        with sprint_metrics_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"| {timestamp} | {remaining_story_points if remaining_story_points is not None else '-'} | "
                f"{blocked_items if blocked_items is not None else '-'} | {summary.replace('|', '/')} |\n"
            )
        return f"Metricas atualizadas: {metrics_path}"

    def sync_daily_tracking(
        self, summary: str, actions_taken: list[str], log_title: str | None = None,
        log_details: str | None = None, metrics_title: str | None = None,
        metrics_summary: str | None = None, source: str = "agent",
    ) -> str:
        control_result = self.update_task_control(summary)
        payload = self._parse_summary(summary)
        log_result = self.append_daily_action_log(
            log_title or "Sincronizacao diaria do sprint",
            log_details or build_default_log_details(payload), source,
        )
        metrics_result = self.record_ai_metrics(
            metrics_title or "Sincronizacao diaria",
            metrics_summary or build_default_log_details(payload).replace("\n", " "),
            actions_taken, payload.get("remaining_story_points"), payload.get("counts", {}).get("blocked_items"),
        )
        return "\n".join([control_result, log_result, metrics_result])

    def read_tracking_snapshot(self) -> str:
        self._ensure_directories()
        paths = {
            "task_control": self.task_control_dir / "current-sprint.json",
            "task_control_markdown": self.task_control_dir / "current-sprint.md",
            "daily_log": self.daily_log_dir / f"{self._today_stamp()}.md",
            "metrics_log": self.metrics_dir / "ai-usage-log.md",
            "sprint_metrics": self.metrics_dir / "sprint-metrics.md",
        }
        snapshot: dict[str, Any] = {name: str(path) for name, path in paths.items()}
        snapshot["exists"] = {name: path.exists() for name, path in paths.items()}
        return json.dumps(snapshot, indent=2, ensure_ascii=True)

    def invoke(self, name: str, arguments: dict[str, Any]) -> str:
        handlers = {
            "sync_daily_tracking": self.sync_daily_tracking,
            "update_task_control": self.update_task_control,
            "append_daily_action_log": self.append_daily_action_log,
            "record_ai_metrics": self.record_ai_metrics,
            "read_tracking_snapshot": self.read_tracking_snapshot,
        }
        if name not in handlers:
            return f"Ferramenta desconhecida: {name}"
        with _TRACKING_LOCK, self._filesystem_lock():
            return handlers[name](**arguments)


async def invoke_tracking_capability(name: str, arguments: dict[str, Any]) -> str:
    return await asyncio.to_thread(TrackingService().invoke, name, arguments)


class FastMcpTrackingSyncAdapter:
    """Native local tracking provider for the FastMCP daily_summary slice."""

    def sync_daily_tracking(self, summary: dict[str, Any], source: str) -> Any:
        return TrackingService().invoke("sync_daily_tracking", {
            "summary": json.dumps(summary, ensure_ascii=True),
            "actions_taken": [
                "langgraph_runtime",
                "fastmcp_daily_summary_provider",
                "fastmcp_local_tracking_adapter",
            ],
            "log_title": "Resumo diario executado pelo runtime LangGraph via FastMCP.",
            "metrics_title": "Resumo diario executado pelo runtime LangGraph via FastMCP.",
            "source": source,
        })