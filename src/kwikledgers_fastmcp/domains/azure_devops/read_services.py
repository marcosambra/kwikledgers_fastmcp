from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .configuration import load_configuration


def read_active_user() -> str:
    env_file = os.environ.get("KWIKLEDGERS_ENV_FILE") or os.environ.get("MCP_ENV_FILE")
    if env_file and Path(env_file).expanduser().is_file():
        load_dotenv(Path(env_file).expanduser())
    else:
        load_configuration()

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