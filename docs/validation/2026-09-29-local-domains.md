# Native Local Domains Validation: 2026-09-29

## Scope and Delivery

Implemented five native tracking capabilities and four native Windows capabilities.
The existing daily-summary tracking and notification adapters now call native
services. Production modules use only the standard library, do not import legacy
servers/helpers, do not import MCP/FastMCP, and never change `sys.path`.

Changes are confined to `domains/local_tracking/**`, `domains/windows/**`,
`tests/test_native_tracking.py`, `tests/test_native_windows.py`, and this document.
Legacy code, shared gateway, server, history, and capability matrix were not edited.
This document records change evidence instead of adding a competing history file.

## Entrypoints

```python
from kwikledgers_fastmcp.domains.local_tracking.services import invoke_tracking_capability
from kwikledgers_fastmcp.domains.windows.services import invoke_windows_capability

tracking_result = await invoke_tracking_capability(name, arguments)
windows_result = await invoke_windows_capability(name, arguments)
```

Both entrypoints return the raw legacy-compatible `str`, not an MCP content object
or gateway envelope. Filesystem and subprocess work is offloaded using
`asyncio.to_thread`. Invalid inputs raise exceptions, as in the legacy `_dispatch`;
the calling transport remains responsible for its error envelope. Unknown names
return `Ferramenta desconhecida: <name>`.

Synchronous services are `TrackingService` and `WindowsService` in the same modules.
`TrackingService.invoke` and its daily-summary adapter serialize filesystem work
with a process-local lock. Direct individual methods and separate OS processes
require caller-managed serialization; there is no cross-process lock or transaction.

| Tracking capability | Required arguments | Optional arguments |
|---|---|---|
| `sync_daily_tracking` | `summary`, `actions_taken` | `log_title`, `log_details`, `metrics_title`, `metrics_summary`, `source="agent"` |
| `update_task_control` | `summary` | None |
| `append_daily_action_log` | `title`, `details` | `source="agent"` |
| `record_ai_metrics` | `entry_title`, `summary`, `actions_taken` | `remaining_story_points=None`, `blocked_items=None` |
| `read_tracking_snapshot` | None | None |

Tracking `summary` is a serialized JSON object for control/sync; the metrics
`summary` is descriptive text. These meanings are intentionally unchanged.

| Windows capability | Required arguments | Optional arguments |
|---|---|---|
| `send_notification` | `title`, `message` | `urgency="normal"` (`normal` or `high`) |
| `create_calendar_event` | `title`, `start`, `end` | `description=""`, `reminder_minutes=30` |
| `get_upcoming_deadlines` | None | `days=7` |
| `schedule_reminder` | `title`, `remind_at`, `message` | None |

## Tracking Artifact Contracts

Root resolution honors exported `KWIKLEDGERS_ROOT_DIR` and expands/resolves it.
Without an override it walks ancestors for the workspace's `agent/` and `.vscode/`
markers. Standalone deployments without those markers must configure the root.
`TrackingService(root=Path(...))` also supports explicit injection. Native services
do not load the legacy `.env`; configuration must already be in the environment.

All artifact text is written as UTF-8. Dates and log timestamps use UTC, matching
the legacy `%Y-%m-%d` and `%Y-%m-%d %H:%M:%SZ` formats.

| Artifact under `AI_Tracking/` | Preserved behavior |
|---|---|
| `Task_Control/current-sprint.json` | Indented JSON, ASCII escaping, trailing newline, original summary plus `returned_items` |
| `Task_Control/current-sprint.md` | Exact headings, labels, ordering, defaults, identity, type/status breakdowns, child load, project progress, warnings, stories, PRs |
| `Daily_Action_Logs/YYYY-MM-DD.md` | One header, appended UTC entries with title, source, and details |
| `Metrics/ai-usage-log.md` | One header, appended usage blocks; nullable metrics omitted; explicit zero preserved |
| `Metrics/sprint-metrics.md` | One table header, appended rows; missing metrics use `-`; summary pipes replaced with `/` |

`returned_items` means any nonempty, changed state for an existing item ID,
including forward transitions, not just regressions. A corrupt existing JSON
snapshot raises rather than silently discarding previous data. A previous JSON
root that is not an object is treated as empty, matching the legacy helper.

Sync deliberately builds default log/metrics details from the original summary
after writing the control snapshot. It does not replace those details with the
newly detected `returned_items`. Optional empty titles/details retain legacy
fallback behavior. Sync is not transactional: earlier artifacts can remain if a
later write fails.

`read_tracking_snapshot` returns an indented JSON string containing exactly:
`task_control`, `task_control_markdown`, `daily_log`, `metrics_log`, `sprint_metrics`,
and `exists`. Each path has a matching boolean in `exists`. It reports paths and
existence, not file contents. It creates the three tracking directories even when
all five files are absent, and does not create the files themselves.

## Windows Runtime and Safety

Windows native selects `powershell`; WSL selects `powershell.exe` using the
`/proc/version` Microsoft marker. Other platforms return an explicit unavailable
integration error without launching a process. Missing executables and OS launch
errors are returned through the legacy action-specific error string.

Each call uses an argument list, `shell=False`, `-NoProfile`, `-NonInteractive`,
`-STA`, and a UTF-16LE `-EncodedCommand`. User text is UTF-8 JSON carried inside a
base64 data literal and read by `ConvertFrom-Json`; it is never interpolated into
PowerShell instructions. This handles ASCII and typographic quotes, dollar signs,
backticks, newlines, markup, and Unicode without treating them as code. Base64 is
not encryption and must not be treated as a privacy boundary.

`days` and `reminder_minutes` must be integers, not strings/floats/booleans;
negative integers remain accepted as in the legacy schemas. Calendar date strings
remain data interpreted by Outlook. `schedule_reminder` still uses
`datetime.fromisoformat`, a `[LEMBRETE] ` prefix, a 15-minute event, and zero-minute
lead time; naive and offset-bearing timestamps are not silently normalized.

| Boundary | Default timeout | Environment override |
|---|---:|---|
| Notification subprocess | 20 seconds | `KWIKLEDGERS_WINDOWS_NOTIFICATION_TIMEOUT_SECONDS` |
| Outlook subprocess | 90 seconds | `KWIKLEDGERS_WINDOWS_OUTLOOK_TIMEOUT_SECONDS` |

Invalid, empty, or nonpositive timeout overrides use the default. Timeout errors
retain exit code 124 and the legacy explanatory message. Output decoding preserves
UTF-8/CP1252/CP850/Latin-1 fallbacks and the legacy CLIXML cleanup behavior.

Notifications retain toast then BalloonTip fallback. Each attempt has its own
timeout, so a failed toast plus failed fallback can take up to twice the configured
notification timeout. BalloonTip uses the legacy 5-second display and 6-second
lifetime, with `Dispose()` guaranteed by `finally`. Normal/high map to short/long
toast duration, extending the native Windows urgency behavior to the unified WSL
path. There is no looping audio, modal prompt, focus stealing, or automatic
classification from message text. Text nodes use `InnerText`, not XML insertion.

Outlook preserves active-instance discovery, COM construction, six retries for
HRESULT -2147418111 with two-second spacing, reminder settings, saving events,
sorted recurring calendar items, local-date restriction, and line-oriented output.
Calendar mutations are not automatically retried after subprocess timeout: the
host may already have saved an event, so callers must check before retrying.

This implementation intentionally uses the PowerShell path on every supported
host, not optional `winotify`/`win32com` Python shortcuts. Calendar success therefore
uses the legacy PowerShell wording `Evento criado no Outlook: ...`, not the
optional Python COM wording `Evento criado: ...`. Other success/error strings and
calendar result formatting follow the legacy PowerShell contract.

## Validation Evidence

Interpreter: Python 3.12.13 from `kl_observatory/.venv/bin/python`.

```sh
PYTHONPATH=kwikledgers_fastmcp/src kl_observatory/.venv/bin/python -m unittest discover -s kwikledgers_fastmcp/tests -p 'test_native_*.py' -v
```

- Tracking: 10 tests passed. Windows: 12 tests passed. Total: 22 tests passed.
- The first tracking edit was immediately followed by its seven focused tests;
  each later implementation edit was also immediately followed by focused tests.
- Tracking comparison executes only extracted undecorated helper functions from
  the live legacy source in a temporary-directory namespace. It does not import
  the server, run transport code, load environment files, or enable legacy audit.
  It compares exact return strings and artifact bytes across reads, state changes,
  log/metrics append operations, defaults, custom sync values, and final existence.
  This comparison is skipped if legacy source is absent outside the workspace.
- A clean subprocess rejects legacy/MCP/FastMCP imports and confirms unchanged
  `sys.path` while importing both native entrypoints.
- Concurrent tracking dispatch preserves 20 entries and a single log header.
- Windows tests mock every PowerShell subprocess. They cover all four dispatch
  names, native/WSL executable choice, default/overridden timeouts, fallback,
  CLIXML/encoding, platform/launch failures, dangerous text as data, integers,
  deadlines, reminder rollover, and daily-summary urgency.
- Editor diagnostics: no errors reported in the five changed Python files.
- No actual Windows, Outlook, notification, Azure, or workspace tracking writes
  were performed. Tracking writes were confined to test temporary directories.

## Commits and Integration Limits

- `3a6362b`: native tracking services, renderer, and focused tests.
- `d7eaff2`: bounded native Windows services and focused tests.
- This validation report is committed separately with a scoped pathspec.

The shared gateway/server remains owned by the main agent. Its atomic capability
routing must select the two native entrypoints; the existing daily-summary adapters
already use them. No new telemetry/audit hooks are injected here. The outer runtime
should record domain, capability, outcome, duration, and timeout without logging
notification text or calendar contents; payload envelopes remain its responsibility.

The two existing adapter tests in `tests/test_server.py` still patch
`ensure_legacy_import_path` and expect calls into fake legacy servers. They require
owner-side migration to native service mocks and are outside this scope. Targeted
attempts to execute those classes stopped during module import because `fastmcp`
is not installed in the selected interpreter. No full transport-suite success is
claimed; the standalone native suite does not need that dependency.

## Specialist Review Limit

No subagent/delegation tool is available in this session, so the mandated
`SE: UX Designer`, `SE: Responsible AI`, and `SE: Tech Writer` consultations could
not be performed. No external specialist approval is claimed. Main-agent review
is still required for those roles.

Manual review addressed their relevant concerns: textual, Unicode-safe
notifications using native OS accessibility surfaces; caller-selected urgency;
bounded lifetimes without repeated sound or modal interruption; no sensitive
payload logging; explicit effects and timeout ambiguity; exact artifact and return
contracts with documented compatibility differences and reproducible checks.
Screen-reader behavior and actual desktop rendering remain unverified because
real Windows notifications were prohibited for this task.

The same delegation limitation applies to architectural, security, context, and
CI specialist consultation. No product-level requirements or UI screens were
introduced; additional product design work is not required for these services.