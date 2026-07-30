from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def parse_legacy_json_dict(payload: str, *, invalid_message: str, root_message: str) -> dict[str, Any]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError(invalid_message) from error
    if not isinstance(parsed, dict):
        raise RuntimeError(root_message)
    return parsed


def resolve_legacy_mcp_path() -> Path:
    explicit_mcp_path = os.environ.get("KWIKLEDGERS_MCP_SERVERS_PATH")
    if explicit_mcp_path:
        candidate = Path(explicit_mcp_path).expanduser().resolve()
        if candidate.is_dir():
            return candidate
        raise RuntimeError(
            "A variavel KWIKLEDGERS_MCP_SERVERS_PATH foi definida, mas nao aponta para um diretorio valido."
        )

    explicit_legacy_root = os.environ.get("KWIKLEDGERS_LEGACY_ROOT")
    if explicit_legacy_root:
        candidate = Path(explicit_legacy_root).expanduser().resolve() / "agent" / "mcp_servers"
        if candidate.is_dir():
            return candidate
        raise RuntimeError(
            "A variavel KWIKLEDGERS_LEGACY_ROOT foi definida, mas o caminho 'agent/mcp_servers' nao foi encontrado nela."
        )

    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "agent" / "mcp_servers"
        if candidate.is_dir():
            return candidate

    raise RuntimeError(
        "Nao foi possivel localizar o runtime legado da KwikLedgers. "
        "Defina KWIKLEDGERS_MCP_SERVERS_PATH com o diretorio 'agent/mcp_servers' "
        "ou KWIKLEDGERS_LEGACY_ROOT com o root do workspace legado."
    )


def ensure_legacy_import_path() -> None:
    mcp_path = str(resolve_legacy_mcp_path())
    if mcp_path not in sys.path:
        sys.path.insert(0, mcp_path)