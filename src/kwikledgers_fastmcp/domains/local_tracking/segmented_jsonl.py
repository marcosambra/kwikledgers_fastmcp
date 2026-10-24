"""Append-only JSONL split into size-capped segments with time-based retention.

The active file keeps its original name. When the next line would push it past
the size cap, it is archived as ``<stem>.partNNNN<suffix>`` (numbers grow with
time) and a new active file starts. Archived segments older than the retention
window are deleted.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

MAX_SEGMENT_BYTES = 5 * 1024 * 1024
RETENTION = timedelta(days=14)

_PROCESS_LOCK = threading.RLock()


class SegmentedJsonl:
    def __init__(
        self, active: Path, *, max_bytes: int | None = None,
        retention: timedelta = RETENTION, timestamp_key: str = "at",
    ) -> None:
        max_bytes = MAX_SEGMENT_BYTES if max_bytes is None else max_bytes
        if max_bytes < 1:
            raise ValueError("max_bytes deve ser positivo.")
        self.active = Path(active)
        self.max_bytes = max_bytes
        self.retention = retention
        self.timestamp_key = timestamp_key
        self._part_name = re.compile(rf"{re.escape(self.active.stem)}\.part(\d+)")

    def _index(self, path: Path) -> int | None:
        if path.suffix != self.active.suffix:
            return None
        match = self._part_name.fullmatch(path.stem)
        return int(match.group(1)) if match else None

    def archived(self) -> list[Path]:
        if not self.active.parent.is_dir():
            return []
        found = [(index, entry) for entry in self.active.parent.iterdir()
                 if (index := self._index(entry)) is not None]
        return [entry for _, entry in sorted(found)]

    def paths(self) -> list[Path]:
        """Every segment, oldest first, ending with the active file when present."""
        return [*self.archived(), *([self.active] if self.active.exists() else [])]

    def _part_path(self, index: int) -> Path:
        return self.active.with_name(f"{self.active.stem}.part{index:04d}{self.active.suffix}")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.active.with_name(f".{self.active.name}.lock")
        with _PROCESS_LOCK, lock_path.open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
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

    def _line_time(self, line: bytes) -> float | None:
        try:
            value = json.loads(line)[self.timestamp_key]
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()

    def _prune(self) -> None:
        cutoff = time.time() - self.retention.total_seconds()
        for path in self.archived():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue

    def _assert_complete(self, message: str) -> None:
        if not self.active.exists() or self.active.stat().st_size == 0:
            return
        with self.active.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                raise ValueError(message)

    def _roll(self) -> None:
        """Archive the active file in parts of at most ``max_bytes``, split between lines."""
        archived = self.archived()
        index = (self._index(archived[-1]) or 0) + 1 if archived else 1
        source_mtime = self.active.stat().st_mtime
        pending: list[tuple[Path, Path]] = []

        def flush(chunk: bytearray, last_line: bytes) -> None:
            nonlocal index
            final = self._part_path(index)
            temporary = final.with_name(f".{final.name}.tmp")
            pending.append((temporary, final))
            temporary.write_bytes(bytes(chunk))
            # Dating the part by its last event keeps retention honest for old data.
            moment = self._line_time(last_line) or source_mtime
            os.utime(temporary, (moment, moment))
            index += 1

        try:
            chunk = bytearray()
            last_line = b""
            with self.active.open("rb") as source:
                for line in source:
                    if chunk and len(chunk) + len(line) > self.max_bytes:
                        flush(chunk, last_line)
                        chunk = bytearray()
                    chunk += line
                    last_line = line
            if chunk:
                flush(chunk, last_line)
            for temporary, final in pending:
                os.replace(temporary, final)
            self.active.unlink()
        finally:
            for temporary, _ in pending:
                temporary.unlink(missing_ok=True)

    def append(
        self, lines: Iterable[bytes], *, fsync: bool = False, partial_error: str | None = None,
    ) -> None:
        """Append newline-terminated lines, rolling segments before the cap is exceeded.

        With ``partial_error``, an active file whose last record is unterminated is
        refused instead of silently joining two records.
        """
        self.active.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            self._prune()
            for line in lines:
                if partial_error is not None:
                    self._assert_complete(partial_error)
                size = self.active.stat().st_size if self.active.exists() else 0
                if size and size + len(line) > self.max_bytes:
                    self._roll()
                    self._prune()
                with self.active.open("ab") as handle:
                    handle.write(line)
                    handle.flush()
                    if fsync:
                        os.fsync(handle.fileno())
