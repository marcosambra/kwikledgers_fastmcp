#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
    printf 'Usage: bash scripts/bootstrap.sh [python-command]\n' >&2
    exit 2
fi
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    printf 'Usage: bash scripts/bootstrap.sh [python-command]\n'
    exit 0
fi

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_command="${1:-python3.10}"
existing_python="$project_dir/.venv/bin/python"
ca_bundle="${SSL_CERT_FILE:-/etc/ssl/certs/ca-certificates.crt}"

if [[ ! -r "$ca_bundle" ]]; then
    printf 'CA bundle is not readable: %s\n' "$ca_bundle" >&2
    exit 1
fi
if ! command -v poetry >/dev/null 2>&1; then
    printf 'Poetry 2.4.1 is required; install it with the configured CA bundle.\n' >&2
    exit 1
fi
if [[ "$(poetry --version)" != "Poetry (version 2.4.1)" ]]; then
    printf 'Poetry 2.4.1 is required to match the lockfile generator.\n' >&2
    exit 1
fi
if [[ -e "$project_dir/.venv" || -L "$project_dir/.venv" ]]; then
    if [[ ! -x "$existing_python" ]]; then
        printf 'Existing virtualenv is unusable; refusing to replace it.\n' >&2
        exit 1
    fi
    if [[ $# -eq 1 ]]; then
        if ! command -v "$python_command" >/dev/null 2>&1; then
            printf 'Python command is not available: %s\n' "$python_command" >&2
            exit 1
        fi
        if [[ "$("$python_command" -c 'import sys; print(sys.version_info[:3])')" != "$("$existing_python" -c 'import sys; print(sys.version_info[:3])')" ]]; then
            printf 'Requested Python differs from the existing virtualenv; refusing to replace it.\n' >&2
            exit 1
        fi
    fi
    python_command="$existing_python"
fi
if ! command -v "$python_command" >/dev/null 2>&1; then
    printf 'Python command is not available: %s\n' "$python_command" >&2
    exit 1
fi

export SSL_CERT_FILE="$ca_bundle"
export REQUESTS_CA_BUNDLE="${REQUESTS_CA_BUNDLE:-$ca_bundle}"
export CURL_CA_BUNDLE="${CURL_CA_BUNDLE:-$ca_bundle}"
export POETRY_VIRTUALENVS_CREATE=true
export POETRY_VIRTUALENVS_IN_PROJECT=true
export POETRY_NO_INTERACTION=1
unset VIRTUAL_ENV PYTHONPATH PYTHONHOME

cd -- "$project_dir"
poetry check --lock
if [[ "$python_command" == "$existing_python" ]]; then
    export VIRTUAL_ENV="$project_dir/.venv"
else
    poetry env use "$python_command"
fi
poetry install --only main
poetry run python -m pip check
poetry run python -c 'from importlib.metadata import version; from kwikledgers_fastmcp.server import build_fastmcp_server; build_fastmcp_server(); print("fastmcp=" + version("fastmcp")); print("mcp=" + version("mcp"))'