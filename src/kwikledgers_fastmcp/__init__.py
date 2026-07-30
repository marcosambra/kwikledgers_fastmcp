from __future__ import annotations

from typing import Any


def build_server_metadata(*args: Any, **kwargs: Any) -> Any:
	from kwikledgers_fastmcp.server import build_server_metadata as _build_server_metadata

	return _build_server_metadata(*args, **kwargs)


__all__ = ["build_server_metadata"]