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
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


EXECUTION_CAPABILITY_KEY_ENV_VAR = "KWIKLEDGERS_FASTMCP_EXECUTION_KEY"
EXECUTION_CAPABILITY_LEDGER_ENV_VAR = "KWIKLEDGERS_FASTMCP_CAPABILITY_LEDGER"
LOCAL_CAPABILITY_SUFFIX = "local-v1"
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

    try:
        encoded_payload, signature = token.split(".", 1)
        local = signature == LOCAL_CAPABILITY_SUFFIX
        if not local or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded_payload):
            raise ValueError("Invalid token encoding")
        payload_bytes = base64.b64decode(_restore_padding(encoded_payload), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise ExecutionCapabilityError("A capability de execução é inválida.") from error

    _require_local_transport()
    try:
        payload = json.loads(payload_bytes, object_pairs_hook=_unique_fields)
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
    if local and (set(payload) != set(required_fields) | {"version", "action"}
                  or type(payload.get("version")) is not int or payload["version"] != 1
                  or not isinstance(payload.get("action"), str) or not payload["action"].strip()):
        raise ExecutionCapabilityError("O contrato da capability local é inválido.")
    if not re.fullmatch(r"[0-9a-f]{64}", payload["payload_digest"]) or not re.fullmatch(
        r"[0-9a-f]{32}", payload["nonce"]
    ):
        raise ExecutionCapabilityError("O digest ou nonce da capability de execução é inválido.")
    if payload.get("capability") != capability_name:
        raise ExecutionCapabilityError("A capability de execução não autoriza esta operação.")
    try:
        arguments_digest = _payload_digest(arguments)
    except (TypeError, ValueError, RecursionError) as error:
        raise ExecutionCapabilityError("O payload solicitado não é JSON válido.") from error
    if payload.get("payload_digest") != arguments_digest:
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
    if local or consume:
        _claim_capability(payload["nonce"], expires_at, local_token=token if local else None, consume=consume)


def _require_local_transport() -> None:
    try:
        from fastmcp.server.dependencies import get_http_request
    except ImportError as error:
        raise ExecutionCapabilityError("Não foi possível verificar o transporte local.") from error
    try:
        get_http_request()
    except RuntimeError as error:
        if str(error) == "No active HTTP request found.":
            return
        raise ExecutionCapabilityError("Não foi possível verificar o transporte local.") from error
    raise ExecutionCapabilityError("Capabilities locais sem assinatura não são aceitas por HTTP.")


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate capability field")
        result[key] = value
    return result


def _payload_digest(arguments: dict[str, Any]) -> str:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
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


def _claim_capability(nonce: str, expires_at: datetime, *, local_token: str | None = None,
                      consume: bool = True) -> None:
    configured_path = os.environ.get(EXECUTION_CAPABILITY_LEDGER_ENV_VAR)
    if configured_path is not None and (not configured_path.strip() or configured_path == ":memory:"):
        raise ExecutionCapabilityError("O ledger de capabilities deve ser persistente.")
    ledger_path = Path(configured_path) if configured_path is not None else DEFAULT_EXECUTION_CAPABILITY_LEDGER
    nonce_digest = hashlib.sha256(nonce.encode("ascii")).hexdigest()
    try:
        _prepare_capability_ledger(ledger_path, create=local_token is None)
        with closing(sqlite3.connect(str(ledger_path), timeout=5)) as connection:
            connection.execute("PRAGMA synchronous = FULL")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                now = datetime.now(timezone.utc)
                if expires_at <= now:
                    raise ExecutionCapabilityError("A capability de execução expirou.")
                if local_token is not None:
                    issued = connection.execute(
                        "SELECT nonce_digest, expires_at FROM local_execution_capabilities WHERE token_digest = ?",
                        (hashlib.sha256(local_token.encode("ascii")).hexdigest(),),
                    ).fetchone()
                    if issued != (nonce_digest, expires_at.timestamp()):
                        raise ExecutionCapabilityError("A capability local não possui registro de emissão válido.")
                    if not consume:
                        return
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


def _prepare_capability_ledger(path: Path, *, create: bool = True) -> None:
    parent = path.absolute().parent
    owner = os.getuid()
    for directory in (*reversed(parent.parents), parent):
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        metadata = directory.lstat()
        trusted_sticky = directory != parent and metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in (0, owner)
                or (directory == parent and metadata.st_uid != owner)
                or (metadata.st_mode & 0o022 and not trusted_sticky)):
            raise ValueError("Capability ledger requires owner-controlled local directories")
    for suffix in ("-journal", "-wal", "-shm"):
        try:
            metadata = path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        _validate_capability_ledger_file(metadata)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            raise
    else:
        _validate_capability_ledger_file(metadata)
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    handle = os.open(path, flags, 0o600)
    try:
        _validate_capability_ledger_file(os.fstat(handle))
        os.fchmod(handle, 0o600)
    finally:
        os.close(handle)


def _validate_capability_ledger_file(metadata: os.stat_result) -> None:
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022 or metadata.st_nlink != 1):
        raise ValueError("Capability ledger requires an owner-controlled local file")