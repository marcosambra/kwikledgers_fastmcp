from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import traceback
import unittest
from datetime import datetime
from unittest.mock import patch

from kwikledgers_fastmcp.domains.windows.services import (
    FastMcpNotificationAdapter, WindowsCapabilityError, WindowsService, _decode_powershell_output,
    _get_timeout_seconds, invoke_windows_capability,
)


class NativeWindowsTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {
            "KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS": "90",
            "KWIKLEDGERS_WINDOWS_NOTIFICATION_TIMEOUT_SECONDS": "20",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.service = WindowsService(is_wsl=True)
        runner = patch("kwikledgers_fastmcp.domains.windows.services.subprocess.run")
        self.runner = runner.start()
        self.addCleanup(runner.stop)
        self.runner.return_value = subprocess.CompletedProcess([], 0, b"OK", b"")

    def script(self, index: int = -1) -> str:
        command = self.runner.call_args_list[index].args[0]
        self.assertEqual(command[0], "powershell.exe")
        self.assertEqual(command[-2], "-EncodedCommand")
        self.assertEqual(self.runner.call_args_list[index].kwargs["shell"], False)
        self.assertEqual(self.runner.call_args_list[index].kwargs["text"], False)
        return base64.b64decode(command[-1]).decode("utf-16le")

    def test_notification_literal_text_unicode_and_urgency(self) -> None:
        title = "Ação ' ’ ‘ ; $(Remove-Item *) #"
        message = "<script>&\nMensagem ' com ` e $ variáveis"
        self.assertEqual(self.service.send_notification(title, message, "high"), f"Notificacao enviada: {title}")
        script = self.script()
        self.assertEqual(self.payload(script), {"title": title, "message": message})
        self.assertIn("InnerText = $payload.title", script)
        self.assertIn("InnerText = $payload.message", script)
        self.assertNotIn("Remove-Item", script)
        self.assertNotIn("<script>", script)
        self.assertIn("SetAttribute('duration', 'long')", script)
        self.assertNotIn("Invoke-Expression", script)
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 20)

    def test_notification_fallback_and_clixml_error(self) -> None:
        self.runner.side_effect = [
            subprocess.CompletedProcess([], 1, b"", b"toast unavailable"),
            subprocess.CompletedProcess([], 1, b"", b'#< CLIXML\n<Objs><S S="Error">COM unavailable_x000D__x000A_</S></Objs>'),
        ]
        self.assertEqual(self.service.send_notification("Title", "Body"), "Erro ao enviar notificacao: COM unavailable")
        self.assertEqual(self.runner.call_count, 2)
        self.assertIn("ShowBalloonTip(5000, $payload.title, $payload.message, 'Info')", self.script())
        self.assertEqual(self.payload(self.script()), {"title": "Title", "message": "Body"})
        self.assertIn("finally", self.script())
        self.assertIn("$n.Dispose()", self.script())

    def test_notification_fallback_success(self) -> None:
        self.runner.side_effect = [subprocess.CompletedProcess([], 1, b"", b"failed"), self.runner.return_value]
        self.assertEqual(self.service.send_notification("Title", "Body"), "Notificacao enviada: Title")
        self.assertIn("SetAttribute('duration', 'short')", self.script(0))

    def test_calendar_defaults_quoting_and_success_contract(self) -> None:
        result = self.service.create_calendar_event("D'água", "2026-09-29T10:00:00", "2026-09-29T11:00:00", "' ; $(x)")
        self.assertEqual(result, "Evento criado no Outlook: 'D'água' em 2026-09-29T10:00:00")
        script = self.script()
        self.assertEqual(self.payload(script), {
            "title": "D'água", "description": "' ; $(x)",
            "start": "2026-09-29T10:00:00", "end": "2026-09-29T11:00:00",
        })
        self.assertIn("$appt.Subject = $payload.title", script)
        self.assertIn("$appt.Body = $payload.description", script)
        self.assertIn("$appt.ReminderMinutesBeforeStart = 30", script)
        self.assertIn("$appt.ReminderSet = $true", script)
        self.assertIn("$appt.Save()", script)
        self.assertIn("$attempts = 6", script)
        self.assertIn("-2147418111", script)
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 90)

    def test_invalid_numbers_dates_and_urgency_never_launch(self) -> None:
        for value in ("0; Remove-Item *", True, 1.5):
            with self.assertRaises(ValueError):
                self.service.create_calendar_event("T", "2026-09-29T10:00:00", "2026-09-29T11:00:00", reminder_minutes=value)
            with self.assertRaises(ValueError):
                self.service.get_upcoming_deadlines(value)
        with self.assertRaises(ValueError):
            self.service.create_calendar_event("T", None, "2026-09-29")
        with self.assertRaises(ValueError):
            self.service.schedule_reminder("T", "invalid", "Message")
        with self.assertRaises(ValueError):
            self.service.send_notification("T", "Message", "high';x")
        self.runner.assert_not_called()

    def test_calendar_dates_are_data_and_negative_integers_keep_legacy_behavior(self) -> None:
        self.service.create_calendar_event("T", "'; Start-Process x", "29/09/2026 11:00", reminder_minutes=-1)
        script = self.script()
        self.assertNotIn("Start-Process", script)
        self.assertEqual(self.payload(script)["start"], "'; Start-Process x")
        self.assertIn("$appt.ReminderMinutesBeforeStart = -1", script)
        self.runner.return_value = subprocess.CompletedProcess([], 0, b"", b"")
        self.assertEqual(self.service.get_upcoming_deadlines(-1), "Nenhum evento nos proximos -1 dias.")

    def test_deadlines_exact_filter_recurrences_and_output(self) -> None:
        with patch("kwikledgers_fastmcp.domains.windows.services.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 29, 10, 15)
            self.runner.return_value = subprocess.CompletedProcess([], 0, "29/09 10:30 | Ação\n30/09 09:00 | Revisão\n".encode(), b"")
            self.assertEqual(self.service.get_upcoming_deadlines(),
                             "Proximos 7 dias:\n  29/09 10:30 | Ação\n  30/09 09:00 | Revisão")
            script = self.script()
            self.assertIn("$items.Sort('[Start]')", script)
            self.assertIn("$items.IncludeRecurrences = $true", script)
            self.assertIn("[Start] >= '09/29/2026 10:15' AND [Start] <= '10/06/2026 10:15'", script)
        self.runner.return_value = subprocess.CompletedProcess([], 0, b"\n", b"")
        self.assertEqual(self.service.get_upcoming_deadlines(0), "Nenhum evento nos proximos 0 dias.")

    def test_reminder_is_fifteen_minutes_zero_lead(self) -> None:
        self.service.schedule_reminder("T", "2026-09-29T23:55:00+02:00", "Mensagem")
        script = self.script()
        self.assertEqual(self.payload(script), {
            "title": "[LEMBRETE] T", "start": "2026-09-29T23:55:00+02:00",
            "end": "2026-09-30T00:10:00+02:00", "description": "Mensagem",
        })
        self.assertIn("$appt.ReminderMinutesBeforeStart = 0", script)

    @staticmethod
    def payload(script: str) -> dict:
        encoded = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)
        if encoded is None:
            raise AssertionError("Missing encoded data payload")
        return json.loads(base64.b64decode(encoded.group(1)).decode("utf-8"))

    def test_timeout_and_environment_overrides(self) -> None:
        self.runner.side_effect = subprocess.TimeoutExpired(["powershell.exe"], 3, output=b"partial")
        with patch.dict(os.environ, {"KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS": "3"}):
            result = self.service.get_upcoming_deadlines()
        self.assertIn("Erro ao ler calendario: PowerShell expirou apos 3 segundos", result)
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 3)
        for raw in ("", "invalid", "0", "-2"):
            with patch.dict(os.environ, {"TIMEOUT_TEST": raw}):
                self.assertEqual(_get_timeout_seconds("TIMEOUT_TEST", 90), 90)

    def test_windows_timeouts_are_bounded_and_invalid_values_use_defaults(self) -> None:
        for default in (20, 90):
            for raw, expected in (("1", 1), (" 120 ", 120), ("301", 300), ("999999", 300),
                                  ("nan", default), ("inf", default), ("1.5", default),
                                  ("secret", default), ("9" * 5000, default)):
                with self.subTest(default=default, raw=raw), patch.dict(os.environ, {"TIMEOUT_TEST": raw}):
                    self.assertEqual(_get_timeout_seconds("TIMEOUT_TEST", default), expected)

    def test_platform_missing_executable_and_native_command(self) -> None:
        self.assertIn("requer Windows ou WSL", WindowsService(is_wsl=False, platform_name="posix").get_upcoming_deadlines())
        self.runner.assert_not_called()
        self.runner.side_effect = FileNotFoundError("powershell.exe")
        self.assertIn("PowerShell indisponivel", self.service.get_upcoming_deadlines())
        self.runner.side_effect = None
        WindowsService(is_wsl=False, platform_name="nt").get_upcoming_deadlines()
        self.assertEqual(self.runner.call_args.args[0][0], "powershell")

    def test_decode_and_error_output_fallback(self) -> None:
        self.assertEqual(_decode_powershell_output(None), "")
        self.assertEqual(_decode_powershell_output("text"), "text")
        self.assertEqual(_decode_powershell_output("Ação".encode("cp1252")), "Ação")
        self.runner.return_value = subprocess.CompletedProcess([], 1, b"failed", b"")
        self.assertEqual(self.service.get_upcoming_deadlines(), "Erro ao ler calendario: failed")

    def test_async_all_capabilities_and_native_adapter(self) -> None:
        with patch("kwikledgers_fastmcp.domains.windows.services._is_wsl", return_value=True):
            for name, arguments in (
                ("send_notification", {"title": "T", "message": "M"}),
                ("create_calendar_event", {"title": "T", "start": "2026-09-29T10:00:00", "end": "2026-09-29T11:00:00"}),
                ("get_upcoming_deadlines", {}),
                ("schedule_reminder", {"title": "T", "remind_at": "2026-09-29T10:00:00", "message": "M"}),
            ):
                self.assertIsInstance(asyncio.run(invoke_windows_capability(name, arguments)), str)
            with patch.object(WindowsService, "send_notification", return_value="OK") as notifier:
                FastMcpNotificationAdapter().send_daily_summary_notification({"counts": {"blocked_items": 1}})
                self.assertEqual(notifier.call_args.args[2], "high")
                FastMcpNotificationAdapter().send_daily_summary_notification({})
                self.assertEqual(notifier.call_args.args[2], "normal")

    def test_unknown_capability_is_value_error_without_dispatch_or_secret(self) -> None:
        with self.assertRaises(ValueError) as raised:
            asyncio.run(invoke_windows_capability("secret", {}))
        self.assertEqual(str(raised.exception), "Unknown Windows capability.")
        self.runner.assert_not_called()

    def test_boundary_rejects_legacy_error_strings_for_every_capability(self) -> None:
        for name in ("send_notification", "create_calendar_event", "schedule_reminder", "get_upcoming_deadlines"):
            with self.subTest(name=name), patch.object(WindowsService, name, return_value="Erro ao executar: secret"), \
                 self.assertRaises(WindowsCapabilityError) as raised:
                asyncio.run(invoke_windows_capability(name, {}))
            self.assertEqual(raised.exception.execution_outcome_unknown, name != "get_upcoming_deadlines")
            self.assertNotIn("secret", str(raised.exception))
        self.runner.assert_not_called()

    def test_boundary_sanitizes_unexpected_exceptions_and_validation(self) -> None:
        for name in ("create_calendar_event", "get_upcoming_deadlines"):
            for error in (RuntimeError("secret"), ValueError("secret"), TypeError("secret")):
                with self.subTest(name=name, error=type(error)), patch.object(WindowsService, name, side_effect=error):
                    try:
                        asyncio.run(invoke_windows_capability(name, {}))
                    except WindowsCapabilityError as failure:
                        self.assertEqual(failure.execution_outcome_unknown,
                                         isinstance(error, RuntimeError) and name != "get_upcoming_deadlines")
                        self.assertNotIn("secret", "".join(traceback.format_exception(failure)))
                    else:
                        self.fail("Windows failure was returned as success")
        self.runner.assert_not_called()

    def test_boundary_validation_never_launches_or_leaks_arguments(self) -> None:
        for name, arguments in (
            ("schedule_reminder", {"title": "T", "remind_at": "secret", "message": "M"}),
            ("create_calendar_event", {"title": "T", "start": "S", "end": "E", "reminder_minutes": "secret"}),
            ("send_notification", {"title": "T", "message": "M", "urgency": "secret"}),
            ("get_upcoming_deadlines", {"secret": "secret"}),
        ):
            with self.subTest(name=name):
                try:
                    asyncio.run(invoke_windows_capability(name, arguments))
                except WindowsCapabilityError as failure:
                    self.assertFalse(failure.execution_outcome_unknown)
                    self.assertNotIn("secret", "".join(traceback.format_exception(failure)))
                else:
                    self.fail("Invalid arguments were returned as success")
        self.runner.assert_not_called()

    def test_boundary_rejects_powershell_timeout_and_failure_without_raw_output(self) -> None:
        for result in (subprocess.CompletedProcess([], 1, b"", b"secret"),
                       subprocess.TimeoutExpired(["secret"], 90, output=b"secret")):
            self.runner.side_effect = result if isinstance(result, Exception) else None
            if not isinstance(result, Exception):
                self.runner.return_value = result
            with patch("kwikledgers_fastmcp.domains.windows.services._is_wsl", return_value=True), \
                 self.assertRaises(WindowsCapabilityError) as raised:
                asyncio.run(invoke_windows_capability("create_calendar_event", {
                    "title": "secret", "start": "2026-10-01T10:00:00", "end": "2026-10-01T11:00:00",
                }))
            self.assertTrue(raised.exception.execution_outcome_unknown)
            self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()