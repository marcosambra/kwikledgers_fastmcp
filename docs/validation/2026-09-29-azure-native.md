# Azure Native Validation - 2026-09-29

## Review Fix Slice 1: Lifecycle Work Item Primitives

- Added structured story and child snapshots with actual SDK revisions,
  `current_status`, work item type, assignment, area and effort fields. The
  original human-readable message remains available without modification.
- Status writes now return the actual SDK work item and fail on a different
  item, missing revision or a state different from the requested state.
- Content updates accept assignment, area and state; effort updates accept
  state alongside supported effort fields. Both use one JSON Patch including
  the revision test. No workflow-state allowlist is introduced.
- Atomic effort/state updates reject unsupported effort rather than closing
  without recording the requested effort. Omitted remaining work is untouched.
- Validation: lifecycle-focused regressions plus native service tests, using
  simulated SDK clients only. Legacy parity compares unchanged human messages
  and excludes additive snapshot fields; the new fields are tested explicitly.
- Files: `domains/azure_devops/services.py`,
  `tests/test_azure_native_services.py` and this scoped history artifact.
- Rationale: lifecycle callers need confirmed machine-readable results and
  atomic task start/closure primitives, not message parsing or two writes.

## Review Fix Slice 2: Required Native Revisions

- The native boundary requires a target revision for content, effort, status,
  comment and next-sprint mutations. Branch and PR execution require a revision
  map covering exactly all resolved parent, child and explicitly related items.
- Branch preview exposes `expected_revisions` with JSON-string IDs. Execution
  preflights every item, including already-linked items, before any writes.
  A PR creation rechecks the targets after branch association and before Git
  creation. Every relation write still carries its own atomic `test /rev`.
- Missing, stale, incomplete or extra target guards fail without initial writes.
  Successful SDK responses must identify the real target with an advanced
  positive integer revision. Revision advancement uses only the returned value,
  never a synthetic increment. Branch/PR execution returns the final map.
- Validation: 54 tests passed with
  `PYTHONPATH=src:tests .venv/bin/python -m unittest test_azure_native_services.NativeServiceTests test_azure_native_entrypoint -q`.
- Files: `domains/azure_devops/{native,primitives,services}.py`,
  `tests/test_azure_native_{services,entrypoint}.py` and this history artifact.
- Multi-item branch/PR writes remain nontransactional. A race after preflight
  may leave earlier successful writes persisted; no automatic retry occurs.

## Review Fix Slice 3: Graph-Owned Context And Final Evidence

- Removed automatic technical-debt registration, context persistence and
  post-create similarity queries. Creation performs only the requested Azure
  creation and returns the actual work item. The old nullable result keys remain;
  ownership is explicit via `context_persistence="moved_to_langgraph"` and
  `similarity_analysis_owner="langgraph"`.
- Similarity reads preserve matches, scores and associated repository items;
  they may read existing context but never write it. `local_context_path` is
  null and `context_persistence` indicates the LangGraph responsibility.
- Removed the unused implicit context writer. The explicit
  `save_technical_debt_preview_cache` capability remains unchanged.
- PR preview also exposes the map at its root. Atomic effort/state writes reject
  even partially unsupported requested effort; no field is silently skipped
  when a state is part of that patch.
- Added regressions for wrong returned item IDs, actual supported SDK fields,
  nonconsecutive revisions and a race after branch association but before Git
  creation. Network connections are blocked in the service fixtures.
- Final validation on the existing FastMCP Python 3.10 environment:

```text
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_azure_native*.py' -q
Ran 62 tests: OK
Editor diagnostics: no errors in the five changed Python files.
```

- The full cross-domain suite was not rerun for this review-only scope. Its
  earlier results below are historical, not a claim about the current dirty
  gateway/server changes. No gateway, issuer, server or LangGraph file was edited.
- Files: `domains/azure_devops/services.py`,
  `tests/test_azure_native_services.py` and this scoped history artifact.

## Exact Lifecycle Integration Contract

Use the public native entrypoint, not the private service handlers:

```python
await invoke_azure_capability(name: str, arguments: dict[str, Any] | None = None)
```

It returns one envelope, already ready for gateway pass-through:

```text
{
  "capability": name,
  "domain": "azure_devops",
  "operation": "read" | "preview" | "execute",
  "data": <the result object below>
}
```

Do not wrap that envelope again and do not parse `message` for lifecycle state.
Revision arguments are consumed by the native boundary and are not passed to
the private SDK service signatures. `expected_revision` is a positive integer;
`expected_revisions` maps item IDs to positive integer revisions. Returned maps
use string keys, suitable for JSON. A one-item map is also accepted for individual
mutations. Conflicting guards for the same ID are rejected.

### Common Work Item Result

Every structured story/content/effort/status result contains these exact common
keys (SDK fields absent from a work item remain null):

```text
{
  "id": int, "revision": positive int,
  "title": str | null, "work_item_type": str | null,
  "state": str | null, "current_status": str | null,
  "assigned_to": str, "area_path": str | null,
  "iteration_path": str | null,
  "story_points": number | null,
  "remaining_work_hours": number | null,
  "completed_work_hours": number | null,
  "original_estimate_hours": number | null,
  "url": str | null
}
```

`state` and `current_status` are aliases of the actual `System.State`.
`assigned_to` is the existing display-name serialization, not necessarily the
email supplied in the request. Revisions are actual SDK revisions, not defaults.

### Story Read And Status

- `get_story_details` arguments: `{"story_id": int}`.
  Data: common result plus `story_id`, `description` (raw HTML or null),
  `acceptance_criteria` (raw HTML or null), `priority`, `tags`,
  `child_work_items` (list of common results for every hierarchy-forward child)
  and the unchanged human-readable `message`. Parent fields/revision/relations
  come from one SDK snapshot. Child snapshots are separate Azure reads.
- `update_story_status` arguments:
  `{"story_id": int, "status": str, "expected_revision": int}`.
  Data: common result plus `story_id`, `requested_status`,
  `status_confirmed: true` and the unchanged human-readable `message`.
  Success guarantees the returned item ID, advanced revision and exact requested
  state. Any valid tenant state is accepted; LangGraph owns transition policy.

### Atomic Start And Closure

`update_work_item_content` arguments:

```text
{
  "work_item_id": int, "expected_revision": int,
  "description": str | null = null,
  "acceptance_criteria": str | null = null,
  "comment": str | null = null,
  "assigned_to": str | null = null,
  "area_path": str | null = null,
  "status": str | null = null
}
```

Data is the common result plus `requested_fields: list[str]`,
`applied_fields: list[str]`, `skipped_fields: list[str]`, `comment_added: bool`,
`description_format: "azure_simple_html" | null` and
`acceptance_criteria_format: "azure_simple_html" | null`.
Assignment, area and state use the actual supported `System.AssignedTo`,
`System.AreaPath` and `System.State` fields. Unsupported fields fail before
writing. All supplied fields and history share one revision-guarded patch.

`update_work_item_effort` arguments:

```text
{
  "work_item_id": int, "expected_revision": int,
  "story_points": number | null = null,
  "remaining_work_hours": number | null = null,
  "completed_work_hours": number | null = null,
  "original_estimate_hours": number | null = null,
  "comment": str | null = null,
  "status": str | null = null
}
```

Data is the common result plus `requested_effort: dict[str, number]`,
`applied_fields: list[str]`, `skipped_fields: list[str]`, `comment_added: bool`.
At least one effort value is required. With `status`, every requested effort
field must be supported. Closure is exactly one invocation with
`completed_work_hours` and `status="Closed"`, producing one JSON Patch containing
`test /rev`, effort fields, `System.State` and optional history. Do not call
`update_story_status` as a second closure write. Omitted/null remaining work
does not add, clear or force its field to zero. A supplied state is verified
against the actual returned item for both content and effort updates.

### Branch And PR Guards

Branch preview arguments are `story_id: int`, `repository_name: str`,
`source_branch: str`, optional `related_work_item_ids: list[int] | null = null`
and `include_child_work_items: bool = true`. Preview data preserves the existing
branch metadata, parent, work items and counts, adding
`expected_revisions: dict[str, int]` for ALL resolved items, including linked ones.
`associate_story_branch` takes those same arguments plus that required map.
Its result preserves preview fields and adds `executed: true`,
`association_result` and the final real `expected_revisions` map.

PR preview/create arguments are the branch arguments plus required strings
`what_was_changed`, `affected_processes`, `expected_impacts`, `important_points`,
`mermaid_diagram`; optional `target_branch: str | null = null`,
`title: str | null = null`, `tests_updated: bool = false`,
`tests_unchanged: bool = false`, `new_library_name: str | null = null`,
`env_changes: str | null = null`, `generated_migration_or_seed: str | null = null`,
`new_queue_or_command: str | null = null`. Creation additionally requires the
preview's `expected_revisions`. Preview data exposes the map both at root and
in `branch_association_preview`. Creation preserves those fields and adds
`executed`, `created`, `pull_request`, `branch_association_result`,
`pull_request_association_result`, `message` and the final root revision map.
Use `data.pull_request.web_url` for the direct PR link. Nested preview metadata
is not the post-write concurrency token; use the final root map or a fresh read.

Invalid/missing guards fail before SDK access with `ValueError`. Stale resolved
snapshots, unsupported fields, mismatched status or SDK errors are surfaced as
`AzureCapabilityError`; its `status_code` retains a real SDK HTTP code when
provided, otherwise null. No success envelope or synthetic HTTP status/revision
is generated on failure. No automatic retry or workflow transition occurs.

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

### Initial Implementation Validation Evidence (Historical)

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
- Technical-debt creation no longer has post-create context/similarity side
  effects, and similarity reads do not write context. LangGraph must explicitly
  orchestrate those responsibilities. The explicit preview-cache capability
  retains legacy last-writer-wins behavior rather than Azure revision guards.
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