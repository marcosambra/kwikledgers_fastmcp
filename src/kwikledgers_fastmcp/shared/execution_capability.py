"""Validate and consume short-lived execute capabilities before dispatch.

Consumption provides at-most-once authorization, not exactly-once execution:
a crash or downstream failure after claiming burns the token. Never retry a
write with a consumed token; reconcile its outcome before seeking new approval.
All workers must share the same durable SQLite ledger on a filesystem that
supports SQLite locking. Separate ledgers, deletion, rollback of the database,
or clock rollback invalidate replay guarantees. No token or secret is stored.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


EXECUTION_CAPABILITY_KEY_ENV_VAR = "KWIKLEDGERS_FASTMCP_EXECUTION_KEY"
EXECUTION_CAPABILITY_LEDGER_ENV_VAR = "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER"
DEFAULT_EXECUTION_CAPABILITY_LEDGER = (
    Path(__file__).resolve().parents[4] / "AI_Tracking/Audit/execution-capabilities.sqlite3"
)


class ExecutionCapabilityError(RuntimeError):
    """Raised when an atomic execute request lacks a valid workflow capability."""


def verify_execution_capability(
    token: str | None,
    *,
    capability_name: str,
    arguments: dict[str, Any],
    consume: bool = True,
) -> None:
    """Consume by default; consume=False validates only and MUST NOT authorize writes."""
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise ExecutionCapabilityError("A capability de execução emitida pelo LangGraph é obrigatória.")

    signing_key = os.environ.get(EXECUTION_CAPABILITY_KEY_ENV_VAR, "").strip()
    if not signing_key:
        raise ExecutionCapabilityError("A chave de capability de execução não está configurada.")

    try:
        encoded_payload, signature = token.split(".", 1)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded_payload) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError("Invalid token encoding")
        payload_bytes = base64.b64decode(_restore_padding(encoded_payload), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise ExecutionCapabilityError("A capability de execução é inválida.") from error

    expected_signature = hmac.new(signing_key.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise ExecutionCapabilityError("A assinatura da capability de execução não confere.")
    try:
        payload = json.loads(payload_bytes)
    except (ValueError, RecursionError) as error:
        raise ExecutionCapabilityError("A capability de execução é inválida.") from error
    required_fields = (
        "capability", "payload_digest", "run_id", "request_id", "nonce", "issued_at", "expires_at"
    )
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(field), str) or not payload[field].strip()
        for field in required_fields
    ):
        raise ExecutionCapabilityError("A capability de execução não informa campos válidos.")
    if not re.fullmatch(r"[0-9a-f]{64}", payload["payload_digest"]) or not re.fullmatch(
        r"[0-9a-f]{32}", payload["nonce"]
    ):
        raise ExecutionCapabilityError("O digest ou nonce da capability de execução é inválido.")
    if payload.get("capability") != capability_name:
        raise ExecutionCapabilityError("A capability de execução não autoriza esta operação.")
    if payload.get("payload_digest") != _payload_digest(arguments):
        raise ExecutionCapabilityError("A capability de execução não corresponde ao payload solicitado.")
    issued_at = _parse_expiration(payload["issued_at"])
    expires_at = _parse_expiration(payload["expires_at"])
    now = datetime.now(timezone.utc)
    if not timedelta(0) < expires_at - issued_at <= timedelta(minutes=5):
        raise ExecutionCapabilityError("O prazo da capability de execução é inválido.")
    if issued_at > now + timedelta(seconds=30):
        raise ExecutionCapabilityError("A capability de execução foi emitida no futuro.")
    if expires_at <= now:
        raise ExecutionCapabilityError("A capability de execução expirou.")
    if consume:
        _claim_capability(payload["nonce"], expires_at)


def _payload_digest(arguments: dict[str, Any]) -> str:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _restore_padding(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def _parse_expiration(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionCapabilityError("A capability de execução não informa expiração.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Timezone is required")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ExecutionCapabilityError("A data da capability de execução é inválida ou não possui fuso.") from error


def _claim_capability(nonce: str, expires_at: datetime) -> None:
    configured_path = os.environ.get(EXECUTION_CAPABILITY_LEDGER_ENV_VAR)
    if configured_path is not None and (not configured_path.strip() or configured_path == ":memory:"):
        raise ExecutionCapabilityError("O ledger de capabilities deve ser persistente.")
    ledger_path = Path(configured_path) if configured_path is not None else DEFAULT_EXECUTION_CAPABILITY_LEDGER
    nonce_digest = hashlib.sha256(nonce.encode("ascii")).hexdigest()
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(ledger_path), timeout=5)) as connection:
            connection.execute("PRAGMA synchronous = FULL")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                now = datetime.now(timezone.utc)
                if expires_at <= now:
                    raise ExecutionCapabilityError("A capability de execução expirou.")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS execution_capabilities "
                    "(nonce_digest TEXT PRIMARY KEY, expires_at REAL NOT NULL)"
                )
                connection.execute("DELETE FROM execution_capabilities WHERE expires_at <= ?", (now.timestamp(),))
                connection.execute(
                    "INSERT INTO execution_capabilities (nonce_digest, expires_at) VALUES (?, ?)",
                    (nonce_digest, expires_at.timestamp()),
                )
    except sqlite3.IntegrityError as error:
        raise ExecutionCapabilityError("A capability de execução já foi consumida.") from error
    except (OSError, sqlite3.Error, ValueError) as error:
        raise ExecutionCapabilityError("Não foi possível consumir a capability de execução com segurança.") from error