# Azure Native Validation - 2026-09-29

## Scope And Change History

All changes stay inside `domains/azure_devops/**`, `tests/test_azure_native*.py`
and this artifact. The legacy `agent/**`, transport server, legacy gateway and
execution-capability verifier are not edited. Existing dirty active-user changes
are preserved. This artifact is also the scoped change-history record, rather
than writing outside the ownership approved for this task.

## Slice 1: Native Configuration

- Added lazy SDK connection construction and independent environment loading.
- Explicit environment files and existing workspace configuration remain usable;
  no legacy Python module or import-path bootstrap is required.
- Validation: 3 configuration tests passed on the existing FastMCP Python 3.10
  environment. The editor-selected observatory Python 3.12 environment failed
  because it does not contain `python-dotenv`; no dependency files were changed.
- Rationale: standalone services must not acquire their configuration by
  importing the legacy server.

## Engineering Consultation

Explore, Context Architect, SE: Architect, SE: Security, SE: DevOps/CI,
SE: Tech Writer and SE: Product Manager delegation tools are not available in
this session. Their consultation was not performed; local contract, security,
validation and documentation checks provide evidence, not a substitute claimed
as external specialist review. SE: UX Designer is not applicable because no UI
is changed. SE: Responsible AI model-behavior review is not applicable to these
deterministic SDK services.

## Execution Boundary

All validation uses simulated Azure clients. No live Azure write is authorized
or performed. Workflow approval, state-transition ordering, branch policy and
human supervision remain the responsibility of the LangGraph caller.

## Slice 2: All 21 Native Services

- Added `services.py` by mechanically extracting the original implementations,
  removing MCP transport, audit decorators, legacy environment imports and
  import-path bootstrap. Formatting, WIQL, HTML, similarity, sprint resolution,
  artifact URIs, local caches and textual/JSON return contracts are retained.
- Added the public async entrypoint
  `kwikledgers_fastmcp.domains.azure_devops.invoke_azure_capability(name, arguments)`.
  Blocking SDK work runs in `asyncio.to_thread`. The return envelope contains
  `capability`, `domain`, `operation` and `data`; textual payloads become
  `{"message": "..."}`. The catalog contains 10 reads, 2 previews and 9 executes.
- Preserved the pre-existing dirty `read_active_user` and `_git_config` bodies.
  `FastMcpDailySummaryProvider` now uses the native daily-summary service.
- Added `primitives.py`: optional `expected_revision` for a target work item and
  `expected_revisions` for per-item guards in branch/PR operations. Guards use
  JSON Patch `test /rev`. Successful writes advance only their own expected
  revision during the invocation; conflicts are surfaced without retries.
  Branch/PR artifact additions also guard the freshly observed revision.
  Preview targets and serialized work-item mutations expose `revision`.
- Invalid arguments and unknown capabilities fail before SDK calls. SDK errors
  redact the configured PAT and its Basic-auth encoding, with exception chaining
  suppressed. `AzureCapabilityError` retains the capability name and SDK HTTP
  status code for technical conflict handling. No execution token or approval
  field is interpreted here.
- Removed target-branch allowlists and the recommended story-state transition
  from native PR payloads. Empty diagram content is accepted; formatting of
  supplied diagram content is retained, not syntax-validated by this service.
  Defaults remain Draft PRs targeting `stage-pre-prod`. LangGraph must enforce
  workflow-specific policy; no LangGraph files were changed in this scope.
- Existing bulk reads now use batches of 75 IDs, and WIQL email quotes are
  escaped. Repository cache keys cannot escape the tracking directory.

### Validation Evidence

```text
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_azure_native*.py' -v
Ran 49 tests: OK

PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 65 tests: 63 passed, 2 errors
```

The 49 native tests include actual service execution against simulated SDK
clients with socket connections blocked, 21 original-function parity subtests,
closed tasks and custom work-item types, children, detailed/light sprint data,
HTML formatting, local caches, PR reuse, artifact links, stale revisions,
multi-write PR revision advancement, 210-item batching and WIQL escaping.
The parity test compiles only original function definitions in an isolated
namespace with fake clients. It does not import or run the legacy server,
dispatcher, audit decorators or transport. Timestamps are fixed; only additive
`revision` fields and the removed governance recommendation are excluded from
payload comparison. A subprocess outside the workspace additionally forbids
legacy/MCP/FastMCP imports and verifies that native invocation leaves `sys.path`
unchanged. Editor diagnostics reported no new errors in the owned files.

The two full-suite errors are
`test_fetch_daily_summary_reads_legacy_payload` and
`test_fetch_daily_summary_rejects_invalid_json` in the existing dirty
`tests/test_server.py`. Both patch the removed `ensure_legacy_import_path`
symbol. They assert the obsolete legacy-provider contract, not native behavior.
That file is outside the approved ownership and was not modified. The full
suite is explicitly NOT green; updating those tests requires expanded file
ownership, not live Azure approval.

### Exact Files

- `src/kwikledgers_fastmcp/domains/azure_devops/__init__.py`
- `src/kwikledgers_fastmcp/domains/azure_devops/configuration.py`
- `src/kwikledgers_fastmcp/domains/azure_devops/native.py`
- `src/kwikledgers_fastmcp/domains/azure_devops/primitives.py`
- `src/kwikledgers_fastmcp/domains/azure_devops/read_services.py`
- `src/kwikledgers_fastmcp/domains/azure_devops/services.py`
- `tests/test_azure_native_configuration.py`
- `tests/test_azure_native_entrypoint.py`
- `tests/test_azure_native_services.py`
- `docs/validation/2026-09-29-azure-native.md`

## Residual Limits And Live Approval Gates

- The server and legacy gateway still route their existing catalog through the
  legacy dispatcher except the already-native active user. Only the native
  entrypoint and daily provider are migrated here. Wiring all transport or
  LangGraph consumers to this entrypoint requires a separately owned change;
  the overall runtime must not be described as fully migrated.
- Native services still depend on `azure-devops`, `msrest`, `python-dotenv` and
  standard-library REST/filesystem/git utilities. They can optionally read the
  existing `agent/.env` configuration; they require no legacy Python code.
- No tenant-level parity is claimed. Real field requirements, type/state names,
  permissions, revision-conflict responses and branch/PR integration need
  controlled live validation. Any live writes require a concrete preview and
  explicit approval in the current conversation.
- Branch/PR operations are multi-write and NOT transactional across work items
  or Git/WIT APIs. A later conflict may leave earlier successful associations
  persisted. The native services do not silently retry conflicts or roll back
  external mutations. Concurrent PR creation must be checked against Azure.
- Technical-debt creation retains post-create cache/similarity side effects;
  those can fail after the Azure item has been created. Local JSON caches retain
  legacy last-writer-wins behavior rather than Azure-style revision guards.
- The legacy PR formatter reports repository-template metadata but renders its
  fixed body sections rather than consuming arbitrary template content. This
  inherited limitation remains; previews and fixture parity do not certify
  arbitrary repository templates.
- Legacy best-effort sprint-goal lookup and metadata fallback are retained.
  Missing permissions or unsupported tenant fields require live read validation.

## Observability And Upgrade Notes

The return envelope provides stable capability/domain/operation identifiers
for the caller's existing LangGraph spans and metrics. Native services do not
write legacy audit logs, persist governance state or import the observatory.
No new telemetry sink was added. A future caller integration can emit
`azure_devops.capability` spans with operation/name, outcome and conflict status,
without recording credentials or raw work-item text. SDK/REST compatibility
follows the dependencies already declared in the package; no dependency change
or tenant schema migration is included.