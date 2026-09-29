from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def read_active_user() -> str:
    env_file = os.environ.get("KWIKLEDGERS_ENV_FILE") or os.environ.get("MCP_ENV_FILE")
    if env_file and Path(env_file).expanduser().is_file():
        load_dotenv(Path(env_file).expanduser())
    else:
        mcp_root = os.environ.get("KWIKLEDGERS_MCP_SERVERS_PATH")
        legacy_root = os.environ.get("KWIKLEDGERS_LEGACY_ROOT")
        if mcp_root:
            mcp_path = Path(mcp_root).expanduser()
        elif legacy_root:
            mcp_path = Path(legacy_root).expanduser() / "agent" / "mcp_servers"
        else:
            mcp_path = next(
                (root / "agent" / "mcp_servers" for start in (Path.cwd(), Path(__file__))
                 for root in (start, *start.parents) if (root / "agent" / "mcp_servers").is_dir()),
                None,
            )
        if mcp_path:
            for candidate in (mcp_path / "azure_devops" / ".env", mcp_path / ".env", mcp_path.parent / ".env"):
                if candidate.is_file():
                    load_dotenv(candidate)
                    break

    email = os.environ.get("AZURE_USER_EMAIL", "").strip()
    if not email:
        email = _git_config("user.email") or ""
    if not email:
        return "Email nao configurado. Configure AZURE_USER_EMAIL no .env ou git config --global user.email seu@email.com"

    name = os.environ.get("AZURE_USER_NAME", "").strip() or _git_config("user.name")
    if not name:
        name = " ".join(re.sub(r"[._-]+", " ", email.split("@", 1)[0]).split()).title()
    return f"Usuario ativo: {name} <{email}>" if name else f"Usuario ativo: {email}"


def _git_config(key: str) -> str | None:
    for command in (("git", "config", key), ("git", "config", "--global", key)):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        if result.stdout.strip():
            return result.stdout.strip()
    return None


class FastMcpDailySummaryProvider:
    """Transport-free native daily summary provider."""

    def fetch_daily_summary(self) -> dict[str, Any]:
        from .native import _invoke

        data = _invoke("get_my_daily_summary", {})["data"]
        if not isinstance(data, dict) or "message" in data:
            raise RuntimeError("O provider FastMCP de resumo diario retornou um payload raiz invalido.")
        return data