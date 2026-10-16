"""Correlation metadata is not business input or execution authorization."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, Iterator, Mapping
from uuid import uuid4

from kwikledgers_fastmcp.domains.local_tracking.request_metrics import _identifier

METADATA_KEY = "kwikledgers/context"
_current: ContextVar[OperationalContext | None] = ContextVar("operational_context", default=None)


@dataclass(frozen=True)
class OperationalContext:
    request_id: str
    workspace_id: str = ""
    session_id: str = ""
    run_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Versão de contexto operacional não suportada.")
        for name, value in asdict(self).items():
            if name != "schema_version":
                _identifier(value, name, optional=name != "request_id")

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> OperationalContext | None:
        values = os.environ if env is None else env
        if not values.get("KWIKLEDGERS_REQUEST_ID"):
            return None
        return cls(**{
            name: values.get(f"KWIKLEDGERS_{name.upper()}", "")
            for name in ("request_id", "workspace_id", "session_id", "run_id", "span_id", "parent_span_id")
        })

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> OperationalContext | None:
        if METADATA_KEY not in metadata:
            return None
        value = metadata[METADATA_KEY]
        if not isinstance(value, dict):
            raise ValueError("Contexto operacional deve ser um objeto.")
        allowed = cls.__dataclass_fields__.keys()
        if set(value) - allowed or "request_id" not in value or "schema_version" not in value:
            raise ValueError("Campos inválidos no contexto operacional.")
        return cls(**value)

    def child(self) -> OperationalContext:
        return OperationalContext(**{
            **asdict(self), "parent_span_id": self.span_id, "span_id": str(uuid4()),
        })

    def metadata(self) -> dict[str, Any]:
        return {METADATA_KEY: asdict(self)}


def current_operational_context() -> OperationalContext | None:
    return _current.get() or OperationalContext.from_environment()


@contextmanager
def bind_operational_context(context: OperationalContext | None) -> Iterator[None]:
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)
