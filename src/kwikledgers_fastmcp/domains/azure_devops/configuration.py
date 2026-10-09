from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def workspace_root() -> Path:
    explicit = os.environ.get("KWIKLEDGERS_ROOT_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    for start in (Path.cwd(), Path(__file__).resolve()):
        for root in (start, *start.parents):
            if (root / "projects").is_dir() and (root / "AGENTS.md").is_file():
                return root
    return Path.cwd()


def load_configuration() -> Path | None:
    explicit = os.environ.get("KWIKLEDGERS_ENV_FILE") or os.environ.get("MCP_ENV_FILE")
    candidates = ([Path(explicit).expanduser()] if explicit else []) + [
        workspace_root() / ".env",
    ]
    loaded = next((candidate for candidate in candidates if candidate.is_file()), None)
    if loaded:
        load_dotenv(loaded)
    bundle = Path("/etc/ssl/certs/ca-certificates.crt")
    if bundle.is_file():
        for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "AZURE_CA_BUNDLE", "CURL_CA_BUNDLE"):
            current = os.environ.get(name)
            if not current or not Path(current).is_file():
                os.environ[name] = str(bundle)
    return loaded


def get_project() -> str:
    load_configuration()
    return os.environ.get("AZURE_PROJECT", "KwikLedgers")


def get_client() -> Any:
    load_configuration()
    from azure.devops.connection import Connection
    from msrest.authentication import BasicAuthentication

    if not os.environ.get("AZURE_ORG_URL") or not os.environ.get("AZURE_PAT"):
        raise ValueError("Azure configuration requires AZURE_ORG_URL and AZURE_PAT.")
    return Connection(base_url=os.environ["AZURE_ORG_URL"], creds=BasicAuthentication("", os.environ["AZURE_PAT"]))