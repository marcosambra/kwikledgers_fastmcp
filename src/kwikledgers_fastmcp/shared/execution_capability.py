from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any


EXECUTION_CAPABILITY_KEY_ENV_VAR = "KWIKLEDGERS_FASTMCP_EXECUTION_KEY"


class ExecutionCapabilityError(RuntimeError):
    """Raised when an atomic execute request lacks a valid workflow capability."""


def verify_execution_capability(
    token: str | None,
    *,
    capability_name: str,
    arguments: dict[str, Any],
) -> None:
    if not token:
        raise ExecutionCapabilityError("A capability de execução emitida pelo LangGraph é obrigatória.")

    signing_key = os.environ.get(EXECUTION_CAPABILITY_KEY_ENV_VAR, "").strip()
    if not signing_key:
        raise ExecutionCapabilityError("A chave de capability de execução não está configurada.")

    try:
        encoded_payload, signature = token.split(".", 1)
        payload_bytes = base64.urlsafe_b64decode(_restore_padding(encoded_payload))
        payload = json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError) as error:
        raise ExecutionCapabilityError("A capability de execução é inválida.") from error

    expected_signature = hmac.new(signing_key.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise ExecutionCapabilityError("A assinatura da capability de execução não confere.")
    if payload.get("capability") != capability_name:
        raise ExecutionCapabilityError("A capability de execução não autoriza esta operação.")
    if payload.get("payload_digest") != _payload_digest(arguments):
        raise ExecutionCapabilityError("A capability de execução não corresponde ao payload solicitado.")
    if _parse_expiration(payload.get("expires_at")) <= datetime.now(timezone.utc):
        raise ExecutionCapabilityError("A capability de execução expirou.")


def _payload_digest(arguments: dict[str, Any]) -> str:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _restore_padding(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def _parse_expiration(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionCapabilityError("A capability de execução não informa expiração.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)