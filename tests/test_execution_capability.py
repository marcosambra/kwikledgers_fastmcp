from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from kwikledgers_fastmcp.shared.execution_capability import (
    DEFAULT_EXECUTION_CAPABILITY_LEDGER,
    EXECUTION_CAPABILITY_KEY_ENV_VAR,
    EXECUTION_CAPABILITY_LEDGER_ENV_VAR,
    ExecutionCapabilityError,
    verify_execution_capability,
)


def _sign(payload: Any, key: bytes = b"test-key") -> str:
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    signature = hmac.new(key, payload_bytes, hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _process_claim(token: str, ledger: str, ready: Any, start: Any, result: Any,
                   signing_key: str = "test-key") -> None:
    os.environ[EXECUTION_CAPABILITY_KEY_ENV_VAR] = signing_key
    os.environ[EXECUTION_CAPABILITY_LEDGER_ENV_VAR] = ledger
    ready.put(True)
    if not start.wait(10):
        result.put("timeout")
        return
    try:
        verify_execution_capability(token, capability_name="update_story_status", arguments={"story_id": 1})
    except ExecutionCapabilityError as error:
        result.put("replay" if "já foi consumida" in str(error) else "error")
    else:
        result.put("claimed")


class ExecutionCapabilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.ledger = Path(temporary_directory.name) / "audit" / "capabilities.sqlite3"
        environment = patch.dict(os.environ, {
            EXECUTION_CAPABILITY_KEY_ENV_VAR: "test-key",
            EXECUTION_CAPABILITY_LEDGER_ENV_VAR: str(self.ledger),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.now = datetime.now(timezone.utc)
        self.arguments = {"story_id": 1}
        self.payload = {
            "capability": "update_story_status",
            "payload_digest": hashlib.sha256(b'{"story_id":1}').hexdigest(),
            "run_id": "run-1",
            "request_id": "request-1",
            "nonce": uuid4().hex,
            "issued_at": self.now.isoformat(),
            "expires_at": (self.now + timedelta(minutes=5)).isoformat(),
        }

    def _verify(self, token: Any, *, consume: bool = True) -> None:
        with patch("kwikledgers_fastmcp.shared.execution_capability.datetime") as clock:
            clock.now.return_value = self.now
            clock.fromisoformat.side_effect = datetime.fromisoformat
            verify_execution_capability(
                token, capability_name="update_story_status", arguments=self.arguments, consume=consume
            )

    def _local_token(self, payload: dict[str, Any] | None = None, *, register: bool = True) -> str:
        payload = {**self.payload, "version": 1, "action": "story_status_transition.execute"} if payload is None else payload
        encoded = _sign(payload).split(".", 1)[0]
        token = f"{encoded}.local-v1"
        if register:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.ledger) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS local_execution_capabilities "
                    "(token_digest TEXT PRIMARY KEY, nonce_digest TEXT UNIQUE NOT NULL, expires_at REAL NOT NULL)"
                )
                connection.execute("INSERT INTO local_execution_capabilities VALUES (?, ?, ?)", (
                    hashlib.sha256(token.encode("ascii")).hexdigest(),
                    hashlib.sha256(payload["nonce"].encode("ascii")).hexdigest(),
                    datetime.fromisoformat(payload["expires_at"]).timestamp(),
                ))
        return token

    def test_registered_local_token_without_key_is_consumed_and_replay_rejected(self) -> None:
        token = self._local_token()
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}):
            self._verify(token)
            with self.assertRaisesRegex(ExecutionCapabilityError, "já foi consumida"):
                self._verify(token)
        stored = self.ledger.read_bytes()
        for sensitive in (token, self.payload["nonce"], "run-1", "request-1"):
            self.assertNotIn(sensitive.encode(), stored)

    def test_local_validation_only_requires_registration_but_does_not_consume(self) -> None:
        token = self._local_token()
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}):
            self._verify(token, consume=False)
            self._verify(token, consume=False)
            with sqlite3.connect(self.ledger) as connection:
                self.assertIsNone(connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'execution_capabilities'"
                ).fetchone())
            self._verify(token)

    def test_unregistered_local_token_is_rejected_even_for_validation_only(self) -> None:
        token = self._local_token(register=False)
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}):
            for consume in (False, True):
                with self.subTest(consume=consume), self.assertRaises(ExecutionCapabilityError):
                    self._verify(token, consume=consume)
        self.assertFalse(self.ledger.exists())

    def test_local_tokens_are_denied_over_http_including_loopback(self) -> None:
        from fastmcp.server.http import _current_http_request
        from starlette.requests import Request

        token = self._local_token()
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}):
            for host in ("127.0.0.1", "198.51.100.1"):
                context_token = _current_http_request.set(Request({
                    "type": "http", "method": "POST", "path": "/mcp", "headers": [], "client": (host, 1234),
                }))
                try:
                    for consume in (False, True):
                        with self.subTest(host=host, consume=consume), self.assertRaisesRegex(ExecutionCapabilityError, "HTTP"):
                            self._verify(token, consume=consume)
                finally:
                    _current_http_request.reset(context_token)
            self._verify(token)

    def test_transport_detection_errors_fail_closed(self) -> None:
        token = self._local_token()
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}), patch(
            "fastmcp.server.dependencies.get_http_request", side_effect=RuntimeError("unexpected context failure"),
        ), self.assertRaisesRegex(ExecutionCapabilityError, "transporte"):
            self._verify(token)

    def test_configured_signing_key_is_ignored(self) -> None:
        token = self._local_token()
        self._verify(token)
        with self.assertRaisesRegex(ExecutionCapabilityError, "consumida"):
            self._verify(token)

    def test_old_signed_token_is_rejected_even_with_its_key(self) -> None:
        with self.assertRaises(ExecutionCapabilityError):
            self._verify(_sign(self.payload))
        self.assertFalse(self.ledger.exists())

    def test_local_contract_is_strict(self) -> None:
        payload = {**self.payload, "version": 1, "action": "story_status_transition.execute"}
        invalid = [{**payload, "version": value} for value in (True, "1", 0, 2, None)]
        invalid += [{**payload, "action": value} for value in (False, "", " ", None)]
        invalid += [{field: value for field, value in payload.items() if field != "version"},
                    {field: value for field, value in payload.items() if field != "action"},
                    {**payload, "unexpected": True}]
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}):
            for changed in invalid:
                with self.subTest(payload=changed), self.assertRaises(ExecutionCapabilityError):
                    self._verify(self._local_token(changed, register=False))
        self.assertFalse(self.ledger.exists())

    def test_local_token_fields_cannot_be_rewritten_to_evade_replay(self) -> None:
        token = self._local_token()
        payload = {**self.payload, "version": 1, "action": "story_status_transition.execute"}
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}):
            self._verify(token)
            for field, value in (("nonce", uuid4().hex), ("run_id", "foreign-run"),
                                 ("request_id", "foreign-request"), ("action", "foreign-action"),
                                 ("expires_at", (self.now + timedelta(minutes=4)).isoformat())):
                with self.subTest(field=field), self.assertRaisesRegex(ExecutionCapabilityError, "registro"):
                    self._verify(self._local_token({**payload, field: value}, register=False))

    def test_local_changed_arguments_do_not_consume_valid_token(self) -> None:
        token = self._local_token()
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}):
            self.arguments = {"story_id": 2}
            with self.assertRaisesRegex(ExecutionCapabilityError, "payload"):
                self._verify(token)
            self.arguments = {"story_id": 1}
            self._verify(token)

    def test_local_expiration_future_and_ttl_are_checked(self) -> None:
        payload = {**self.payload, "version": 1, "action": "story_status_transition.execute"}
        invalid = (
            {**payload, "expires_at": self.now.isoformat()},
            {**payload, "expires_at": (self.now + timedelta(minutes=6)).isoformat()},
            {**payload, "issued_at": (self.now + timedelta(minutes=1)).isoformat()},
            {**payload, "issued_at": self.now.replace(tzinfo=None).isoformat()},
        )
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}):
            for changed in invalid:
                with self.subTest(payload=changed), self.assertRaises(ExecutionCapabilityError):
                    self._verify(self._local_token(changed, register=False))
        self.assertFalse(self.ledger.exists())

    def test_local_failed_dispatch_burns_token(self) -> None:
        token = self._local_token()
        write = Mock(side_effect=RuntimeError("downstream outcome unknown"))
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}):
            self._verify(token)
            with self.assertRaises(RuntimeError):
                write()
            with self.assertRaisesRegex(ExecutionCapabilityError, "já foi consumida"):
                self._verify(token)
        write.assert_called_once_with()

    def test_local_gateway_dispatches_exact_arguments_once(self) -> None:
        from kwikledgers_fastmcp.shared.capability_gateway import CapabilityDefinition, invoke_capability

        token = self._local_token()
        definition = CapabilityDefinition("update_story_status", "azure_devops", "execute", "test")
        dispatch = AsyncMock(return_value={"status": "updated"})
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}), patch(
            "kwikledgers_fastmcp.shared.capability_gateway.invoke_azure_capability", new=dispatch,
        ):
            result = asyncio.run(invoke_capability(definition, {**self.arguments, "execution_capability": token}))
            self.assertEqual(result["data"], {"status": "updated"})
            with self.assertRaisesRegex(ExecutionCapabilityError, "já foi consumida"):
                asyncio.run(invoke_capability(definition, {**self.arguments, "execution_capability": token}))
        dispatch.assert_awaited_once_with("update_story_status", self.arguments)

    def test_local_gateway_blocks_http_before_dispatch(self) -> None:
        from kwikledgers_fastmcp.shared.capability_gateway import CapabilityDefinition, invoke_capability

        token = self._local_token()
        definition = CapabilityDefinition("update_story_status", "azure_devops", "execute", "test")
        dispatch = AsyncMock()
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: ""}), patch(
            "fastmcp.server.dependencies.get_http_request", return_value=object(),
        ), patch("kwikledgers_fastmcp.shared.capability_gateway.invoke_azure_capability", new=dispatch):
            with self.assertRaisesRegex(ExecutionCapabilityError, "HTTP"):
                asyncio.run(invoke_capability(definition, {**self.arguments, "execution_capability": token}))
        dispatch.assert_not_awaited()

    def test_valid_token_is_consumed_and_replay_is_rejected(self) -> None:
        token = self._local_token()
        self._verify(token)
        with self.assertRaisesRegex(ExecutionCapabilityError, "já foi consumida"):
            self._verify(token)
        with sqlite3.connect(self.ledger) as connection:
            rows = connection.execute("SELECT * FROM execution_capabilities").fetchall()
        self.assertEqual(rows, [(
            hashlib.sha256(self.payload["nonce"].encode("ascii")).hexdigest(),
            datetime.fromisoformat(self.payload["expires_at"]).timestamp(),
        )])
        stored = self.ledger.read_bytes()
        for secret in (token, "test-key", self.payload["nonce"], "run-1", "request-1"):
            self.assertNotIn(secret.encode("utf-8"), stored)

    def test_validation_only_does_not_consume_registered_token(self) -> None:
        token = self._local_token()
        self._verify(token, consume=False)
        self._verify(token, consume=False)
        self.assertTrue(self.ledger.exists())
        self._verify(token)

    def test_failed_write_does_not_allow_retry_after_consumption(self) -> None:
        token = self._local_token()
        write = Mock(side_effect=RuntimeError("downstream outcome unknown"))

        def execute() -> None:
            self._verify(token)
            write()

        with self.assertRaisesRegex(RuntimeError, "downstream outcome unknown"):
            execute()
        with self.assertRaisesRegex(ExecutionCapabilityError, "já foi consumida"):
            execute()
        write.assert_called_once_with()

    def test_malformed_token_types_and_encodings_fail_closed(self) -> None:
        for token in (None, False, 1, [], {}, b"token", "", "a", "a.b.c", "!." + "0" * 64,
                      "é." + "0" * 64, "a." + "é" * 64, "a" * 16385):
            with self.subTest(token=repr(token)), self.assertRaises(ExecutionCapabilityError):
                self._verify(token)
        self.assertFalse(self.ledger.exists())

    def test_correctly_signed_invalid_json_and_utf8_fail_closed(self) -> None:
        for payload_bytes in (b"{", b"\xff"):
            encoded = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
            signature = hmac.new(b"test-key", payload_bytes, hashlib.sha256).hexdigest()
            with self.subTest(payload=payload_bytes), self.assertRaises(ExecutionCapabilityError):
                self._verify(f"{encoded}.{signature}")
        self.assertFalse(self.ledger.exists())

    def test_signed_non_object_payloads_fail_closed(self) -> None:
        for payload in (None, False, 1, "text", [], [self.payload]):
            with self.subTest(payload=payload), self.assertRaises(ExecutionCapabilityError):
                self._verify(_sign(payload))
        self.assertFalse(self.ledger.exists())

    def test_every_required_field_must_be_a_nonempty_string(self) -> None:
        for field in self.payload:
            missing = dict(self.payload)
            del missing[field]
            with self.subTest(field=field, missing=True), self.assertRaises(ExecutionCapabilityError):
                self._verify(_sign(missing))
            for value in (None, False, 1, [], {}, "", "   "):
                with self.subTest(field=field, value=value), self.assertRaises(ExecutionCapabilityError):
                    self._verify(_sign({**self.payload, field: value}))
        self.assertFalse(self.ledger.exists())

    def test_invalid_signature_operation_and_digest_do_not_consume(self) -> None:
        invalid_tokens = (
            _sign(self.payload, b"wrong-key"),
            _sign({**self.payload, "capability": "delete_story"}),
            _sign({**self.payload, "payload_digest": "0" * 64}),
            _sign({**self.payload, "payload_digest": "not-a-digest"}),
            _sign({**self.payload, "nonce": "not-a-nonce"}),
        )
        for token in invalid_tokens:
            with self.subTest(token=token), self.assertRaises(ExecutionCapabilityError):
                self._verify(token)
        self.assertFalse(self.ledger.exists())
        self._verify(self._local_token())

    def test_malformed_naive_and_overflowing_dates_fail_closed(self) -> None:
        for field in ("issued_at", "expires_at"):
            for value in ("not-a-date", "2026-09-29", "2026-09-29T12:00:00",
                          "2026-02-30T00:00:00Z", "0001-01-01T00:00:00+01:00",
                          "9999-12-31T23:59:59-01:00"):
                with self.subTest(field=field, value=value), self.assertRaises(ExecutionCapabilityError):
                    self._verify(_sign({**self.payload, field: value}))
        self.assertFalse(self.ledger.exists())

    def test_ttl_and_expiry_boundaries(self) -> None:
        invalid_dates = (
            (self.now, self.now),
            (self.now, self.now - timedelta(seconds=1)),
            (self.now, self.now + timedelta(minutes=5, microseconds=1)),
            (self.now - timedelta(minutes=5), self.now),
            (self.now - timedelta(minutes=6), self.now - timedelta(minutes=1)),
            (self.now + timedelta(seconds=30, microseconds=1), self.now + timedelta(minutes=1)),
        )
        for issued_at, expires_at in invalid_dates:
            with self.subTest(issued_at=issued_at, expires_at=expires_at), self.assertRaises(ExecutionCapabilityError):
                self._verify(_sign({
                    **self.payload, "issued_at": issued_at.isoformat(), "expires_at": expires_at.isoformat()
                }))
        self.assertFalse(self.ledger.exists())
        self._verify(self._local_token({**self.payload, "version": 1, "action": "story_status_transition.execute",
                           "issued_at": (self.now + timedelta(seconds=30)).isoformat()}))

    def test_aware_offsets_and_z_are_accepted(self) -> None:
        for offset in (timezone.utc, timezone(timedelta(hours=-3)), timezone(timedelta(hours=5, minutes=30))):
            with self.subTest(offset=offset):
                self._verify(self._local_token({
                    **self.payload,
                    "version": 1, "action": "story_status_transition.execute",
                    "nonce": uuid4().hex,
                    "issued_at": self.now.astimezone(offset).isoformat().replace("+00:00", "Z"),
                    "expires_at": (self.now + timedelta(minutes=5)).astimezone(offset).isoformat().replace("+00:00", "Z"),
                }))

    def test_expired_rows_are_cleaned_but_live_claims_remain(self) -> None:
        token = self._local_token()
        self._verify(token)
        with sqlite3.connect(self.ledger) as connection:
            connection.execute("INSERT INTO execution_capabilities VALUES (?, ?)", ("expired", self.now.timestamp()))
        self._verify(self._local_token({**self.payload, "version": 1, "action": "story_status_transition.execute",
                           "nonce": uuid4().hex}))
        with sqlite3.connect(self.ledger) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM execution_capabilities").fetchone()[0], 2)
            self.assertIsNone(connection.execute(
                "SELECT nonce_digest FROM execution_capabilities WHERE nonce_digest = ?", ("expired",)
            ).fetchone())
        with self.assertRaisesRegex(ExecutionCapabilityError, "já foi consumida"):
            self._verify(token)

    def test_request_rewrite_cannot_reuse_registered_nonce(self) -> None:
        token = self._local_token()
        self._verify(token)
        with self.assertRaisesRegex(ExecutionCapabilityError, "registro"):
            self._verify(self._local_token({**self.payload, "version": 1, "action": "story_status_transition.execute",
                                           "request_id": "another-request"}, register=False))

    def test_storage_failures_and_nonpersistent_paths_fail_closed(self) -> None:
        token = self._local_token()
        for ledger in ("", ":memory:", str(self.ledger.parent.parent)):
            with self.subTest(ledger=ledger), patch.dict(os.environ, {EXECUTION_CAPABILITY_LEDGER_ENV_VAR: ledger}):
                with self.assertRaises(ExecutionCapabilityError):
                    self._verify(token)
        with patch("kwikledgers_fastmcp.shared.execution_capability.sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(ExecutionCapabilityError):
                self._verify(token)
        self._verify(token)

    def test_expiry_is_rechecked_inside_claim(self) -> None:
        token = self._local_token()
        with patch("kwikledgers_fastmcp.shared.execution_capability.datetime") as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.side_effect = [self.now, self.now + timedelta(minutes=5)]
            with self.assertRaisesRegex(ExecutionCapabilityError, "expirou"):
                verify_execution_capability(
                    token, capability_name="update_story_status", arguments=self.arguments
                )
        self._verify(token)

    def test_missing_signing_key_fails_closed(self) -> None:
        with patch.dict(os.environ, {EXECUTION_CAPABILITY_KEY_ENV_VAR: " "}):
            with self.assertRaises(ExecutionCapabilityError):
                self._verify(_sign(self.payload))
        self.assertFalse(self.ledger.exists())

    def test_default_ledger_is_workspace_audit_path(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        self.assertEqual(DEFAULT_EXECUTION_CAPABILITY_LEDGER, workspace / "AI_Tracking/Audit/execution-capabilities.sqlite3")

    def _claims_in_processes(self, token: str, count: int, *, signing_key: str = "test-key") -> list[str]:
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        result = context.Queue()
        start = context.Event()
        processes = [context.Process(
            target=_process_claim, args=(token, str(self.ledger), ready, start, result, signing_key)
        ) for _ in range(count)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=15))
            start.set()
            outcomes = [result.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)
            return outcomes
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                if process.pid is not None:
                    process.close()
            ready.close()
            result.close()

    def test_concurrent_process_claims_have_exactly_one_winner(self) -> None:
        self.assertCountEqual(self._claims_in_processes(self._local_token(), 4), ["claimed", "replay", "replay", "replay"])

    def test_replay_is_rejected_after_process_restart(self) -> None:
        token = self._local_token()
        self.assertEqual(self._claims_in_processes(token, 1), ["claimed"])
        self.assertEqual(self._claims_in_processes(token, 1), ["replay"])

    def test_local_concurrent_claims_have_exactly_one_winner(self) -> None:
        self.assertCountEqual(self._claims_in_processes(self._local_token(), 4, signing_key=""),
                              ["claimed", "replay", "replay", "replay"])

    def test_local_replay_is_rejected_after_process_restart(self) -> None:
        token = self._local_token()
        self.assertEqual(self._claims_in_processes(token, 1, signing_key=""), ["claimed"])
        self.assertEqual(self._claims_in_processes(token, 1, signing_key=""), ["replay"])


if __name__ == "__main__":
    unittest.main()