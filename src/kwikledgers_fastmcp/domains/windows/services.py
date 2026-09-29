from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _get_timeout_seconds(env_name: str, default: int) -> int:
    try:
        value = int(os.environ.get(env_name, str(default)).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Esperado texto para a integracao Windows.")
    return value


def _integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} deve ser um inteiro.")
    return value


def _decode_powershell_output(raw_output: bytes | str | None) -> str:
    if raw_output is None:
        return ""
    if isinstance(raw_output, str):
        return raw_output
    for encoding in ("utf-8", "cp1252", "cp850", "latin-1"):
        try:
            return raw_output.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_output.decode("utf-8", errors="replace")


def _sanitize_powershell_output(text: str) -> str:
    cleaned = text.replace("#< CLIXML", "").replace("_x000D__x000A_", "\n")
    error_lines = re.findall(r'<S S="Error">(.*?)</S>', cleaned, flags=re.S)
    if error_lines:
        cleaned = "\n".join(error_lines)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _format_powershell_error(action: str, result: subprocess.CompletedProcess) -> str:
    details = _sanitize_powershell_output(
        _decode_powershell_output(result.stderr)
        or _decode_powershell_output(result.stdout)
        or "Falha sem detalhes retornados pelo PowerShell."
    )
    return f"Erro ao {action}: {details}"


def _compose_powershell_script(
    body: str, include_outlook_helper: bool = False, payload: dict[str, Any] | None = None,
) -> str:
    prelude = [
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        "$OutputEncoding = [System.Text.Encoding]::UTF8",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
    ]
    if payload is not None:
        encoded_payload = base64.b64encode(json.dumps(payload, ensure_ascii=True).encode("utf-8")).decode("ascii")
        prelude.append(
            "$payload = [System.Text.Encoding]::UTF8.GetString("
            f"[System.Convert]::FromBase64String('{encoded_payload}')) | ConvertFrom-Json"
        )
    if include_outlook_helper:
        prelude.extend([
            "function Get-OutlookApplication {",
            "    $attempts = 6",
            "    for ($attempt = 1; $attempt -le $attempts; $attempt++) {",
            "        try {",
            "            try {",
            "                return [System.Runtime.InteropServices.Marshal]::GetActiveObject('Outlook.Application')",
            "            } catch {",
            "                return New-Object -ComObject Outlook.Application",
            "            }",
            "        } catch [System.Runtime.InteropServices.COMException] {",
            "            if ($_.Exception.HResult -eq -2147418111 -and $attempt -lt $attempts) {",
            "                Start-Sleep -Seconds 2",
            "                continue",
            "            }",
            "            throw",
            "        }",
            "    }",
            "    throw 'Nao foi possivel obter a automacao do Outlook apos varias tentativas.'",
            "}",
        ])
    prelude.append(body.strip())
    return "\n".join(prelude)


class WindowsService:
    """Transport-free Windows/WSL integration through bounded PowerShell calls."""

    def __init__(self, *, is_wsl: bool | None = None, platform_name: str | None = None) -> None:
        self.is_wsl = _is_wsl() if is_wsl is None else is_wsl
        self.platform_name = os.name if platform_name is None else platform_name

    def _run_powershell(self, script: str, timeout_seconds: int) -> subprocess.CompletedProcess:
        shell_name = "powershell.exe" if self.is_wsl else "powershell"
        command = [
            shell_name, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-STA", "-EncodedCommand",
            base64.b64encode(script.encode("utf-16le")).decode("ascii"),
        ]
        if self.platform_name != "nt" and not self.is_wsl:
            return subprocess.CompletedProcess(command, 127, b"", "Integracao Windows indisponivel: requer Windows ou WSL.")
        try:
            return subprocess.run(command, capture_output=True, text=False, timeout=timeout_seconds, shell=False)
        except subprocess.TimeoutExpired as error:
            return subprocess.CompletedProcess(
                error.cmd, 124, _decode_powershell_output(error.stdout),
                f"PowerShell expirou apos {timeout_seconds} segundos ao executar a integracao Windows. "
                "O Outlook pode estar preso em primeiro acesso, perfil MAPI bloqueado ou COM indisponivel no host.",
            )
        except OSError as error:
            return subprocess.CompletedProcess(command, 127, b"", f"PowerShell indisponivel: {error}")

    def send_notification(self, title: str, message: str, urgency: str = "normal") -> str:
        if urgency not in ("normal", "high"):
            raise ValueError("urgency deve ser normal ou high.")
        payload = {"title": _text(title), "message": _text(message)}
        timeout = _get_timeout_seconds("KWIKLEDGERS_WINDOWS_NOTIFICATION_TIMEOUT_SECONDS", 20)
        duration = "long" if urgency == "high" else "short"
        script = _compose_powershell_script(f"""
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
            [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $template.DocumentElement.SetAttribute('duration', '{duration}')
        $template.SelectSingleNode('//text[@id=1]').InnerText = $payload.title
        $template.SelectSingleNode('//text[@id=2]').InnerText = $payload.message
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('KwikLedgers').Show($toast)
        Write-Output 'OK'
        """, payload=payload)
        result = self._run_powershell(script, timeout)
        if result.returncode != 0:
            fallback = _compose_powershell_script(f"""
            Add-Type -AssemblyName System.Windows.Forms
            $n = New-Object System.Windows.Forms.NotifyIcon
            try {{
                $n.Icon = [System.Drawing.SystemIcons]::Information
                $n.Visible = $true
                $n.ShowBalloonTip(5000, $payload.title, $payload.message, 'Info')
                Start-Sleep -Seconds 6
            }} finally {{
                $n.Dispose()
            }}
            Write-Output 'OK'
            """, payload=payload)
            fallback_result = self._run_powershell(fallback, timeout)
            if fallback_result.returncode != 0:
                return _format_powershell_error("enviar notificacao", fallback_result)
        return f"Notificacao enviada: {title}"

    def create_calendar_event(
        self, title: str, start: str, end: str, description: str = "", reminder_minutes: int = 30,
    ) -> str:
        reminder_minutes = _integer(reminder_minutes, "reminder_minutes")
        payload = {
            "title": _text(title), "description": _text(description),
            "start": _text(start), "end": _text(end),
        }
        timeout = _get_timeout_seconds("KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS", 90)
        script = _compose_powershell_script(f"""
        $outlook = Get-OutlookApplication
        $appt = $outlook.CreateItem(1)
        $appt.Subject = $payload.title
        $appt.Body = $payload.description
        $appt.Start = $payload.start
        $appt.End = $payload.end
        $appt.ReminderMinutesBeforeStart = {reminder_minutes}
        $appt.ReminderSet = $true
        $appt.Save()
        Write-Output 'OK'
        """, include_outlook_helper=True, payload=payload)
        result = self._run_powershell(script, timeout)
        if result.returncode != 0:
            return _format_powershell_error("criar evento no Outlook", result)
        return f"Evento criado no Outlook: '{title}' em {start}"

    def get_upcoming_deadlines(self, days: int = 7) -> str:
        days = _integer(days, "days")
        now = datetime.now()
        end = now + timedelta(days=days)
        timeout = _get_timeout_seconds("KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS", 90)
        script = _compose_powershell_script(f"""
        $outlook = Get-OutlookApplication
        $ns = $outlook.GetNamespace('MAPI')
        $cal = $ns.GetDefaultFolder(9)
        $items = $cal.Items
        $items.Sort('[Start]')
        $items.IncludeRecurrences = $true
        $filter = "[Start] >= '{now.strftime('%m/%d/%Y %H:%M')}' AND [Start] <= '{end.strftime('%m/%d/%Y %H:%M')}'"
        $restricted = $items.Restrict($filter)
        foreach ($item in $restricted) {{
            Write-Output "$($item.Start.ToString('dd/MM HH:mm')) | $($item.Subject)"
        }}
        """, include_outlook_helper=True)
        result = self._run_powershell(script, timeout)
        if result.returncode != 0:
            return _format_powershell_error("ler calendario", result)
        lines = _decode_powershell_output(result.stdout).strip().splitlines()
        if not lines:
            return f"Nenhum evento nos proximos {days} dias."
        return f"Proximos {days} dias:\n" + "\n".join(f"  {line}" for line in lines)

    def schedule_reminder(self, title: str, remind_at: str, message: str) -> str:
        start = datetime.fromisoformat(remind_at)
        return self.create_calendar_event(
            title=f"[LEMBRETE] {title}", start=start.isoformat(),
            end=(start + timedelta(minutes=15)).isoformat(), description=message, reminder_minutes=0,
        )


async def invoke_windows_capability(name: str, arguments: dict[str, Any]) -> str:
    service = WindowsService()
    handlers = {
        "send_notification": service.send_notification,
        "create_calendar_event": service.create_calendar_event,
        "get_upcoming_deadlines": service.get_upcoming_deadlines,
        "schedule_reminder": service.schedule_reminder,
    }
    if name not in handlers:
        return f"Ferramenta desconhecida: {name}"
    return await asyncio.to_thread(handlers[name], **arguments)


class FastMcpNotificationAdapter:
    """Native notification provider for the FastMCP daily_summary slice."""

    def send_daily_summary_notification(self, summary: dict[str, Any]) -> Any:
        counts = summary.get("counts", {})
        blocked_items = counts.get("blocked_items", 0)
        urgency = "high" if blocked_items else "normal"
        remaining_story_points = summary.get("remaining_story_points", "-")
        message = (
            "Resumo diario sincronizado pelo runtime LangGraph via FastMCP. "
            f"Itens atribuidos: {counts.get('assigned_items', 0)}. "
            f"Bloqueados: {blocked_items}. "
            f"PRs abertas: {counts.get('open_prs', 0)}. "
            f"Story points restantes: {remaining_story_points}."
        )
        return WindowsService().send_notification(
            "Resumo diario disponivel",
            message,
            urgency,
        )