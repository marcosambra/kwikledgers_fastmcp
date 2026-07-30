# FastMCP Migration Roadmap

Roadmap for migrating the current MCP runtime into `kwikledgers_fastmcp/` while keeping `kwikledgers_langgraph/` as the owner of workflow, preview-first policy, approval replay, and resumable execution.

## Migration Order

1. Freeze contracts and compatibility seams.
2. Extract reusable domain services from the current Azure MCP.
3. Migrate Azure read-only capabilities to FastMCP.
4. Migrate local tracking to FastMCP.
5. Migrate Azure preview primitives to FastMCP.
6. Migrate Azure execute primitives to FastMCP.
7. Introduce LangGraph adapter switching between legacy and FastMCP.
8. Migrate Windows last.
9. Sunset the legacy MCP runtime after a bounded parallel window.

## Slices

### Slice 0. Contract Freeze

Goal:
Freeze the current payload contracts that LangGraph already depends on.

FastMCP scope:
- no implementation yet;
- define parity targets for existing payloads.

LangGraph scope:
- remain unchanged;
- document which preview payload fields are required for replay.

Risks:
- replay regressions caused by accidental payload drift;
- hidden dependencies on field order, names, or minimal preview content.

Validation:
- golden fixtures for `daily_summary`, branch-association preview, story PR preview, and mutable-flow state expectations.

Status update:
- A first LangGraph backend selector seam now exists and still defaults to `legacy`.
- The `kwikledgers_fastmcp/` package scaffold now exists, but all capabilities remain intentionally unimplemented.

### Slice 1. Azure Service Extraction

Goal:
Split the current Azure MCP into transport-free domain services before any FastMCP transport migration.

FastMCP scope:
- define the service boundaries the new runtime will call.

LangGraph scope:
- continue using the current legacy adapters;
- prepare to consume either legacy or FastMCP through the same ports.

Risks:
- moving logic without tests around the largest current concentration point;
- carrying policy accidentally into the new service layer.

Validation:
- contract tests comparing legacy tool payloads against extracted-service payloads.

### Slice 2. Azure Read-Only FastMCP

Goal:
Move non-mutating Azure capabilities first.

FastMCP scope:
- active user;
- work-item reads;
- blocked items;
- daily summary;
- sprint stories;
- story details;
- open PR reads.

LangGraph scope:
- optionally switch read-only adapters behind a feature flag;
- keep mutable flows on legacy adapters.

Risks:
- payload normalization differences;
- stale compatibility gaps during shadow comparison.

Validation:
- shadow mode comparing legacy and FastMCP JSON output after normalization;
- focused contract tests per tool.

Status update:
- The first FastMCP read-only capability is now wired for `daily_summary` through a legacy-compatible Azure provider.
- `kwikledgers_langgraph/` can already route `daily_summary` through `fastmcp`, while local tracking and notifications remain hybrid on legacy adapters in this slice.

### Slice 3. Local Tracking FastMCP

Goal:
Move the simplest write-heavy domain after Azure reads stabilize.

FastMCP scope:
- `sync_daily_tracking`;
- `update_task_control`;
- `append_daily_action_log`;
- `record_ai_metrics`;
- `read_tracking_snapshot`.

LangGraph scope:
- keep deciding when tracking is executed inside each flow.

Risks:
- markdown drift in `AI_Tracking` artifacts;
- implicit assumptions about file formatting.

Validation:
- snapshot comparison of generated files;
- minimal-content assertions for markdown outputs.

Status update:
- The `daily_summary` path now also routes local tracking writes through a FastMCP adapter bridge.
- Windows notifications remain outside this slice, so the LangGraph `daily_summary` flow is still hybrid only at the notification edge.

### Slice 4. Azure Preview Primitives

Goal:
Move deterministic Azure preview builders without moving approval policy.

FastMCP scope:
- branch-association preview helpers;
- pull-request preview helpers;
- technical preview payload assembly for execute primitives.

LangGraph scope:
- continue owning `pending_approval`, `approval_confirmation`, replay checks, and flow sequencing;
- keep status-transition preview logic as a workflow concern unless a new atomic story-status snapshot tool is introduced.

Risks:
- leaking workflow decisions back into preview builders;
- treating a preview as approval proof.

Validation:
- graph tests for branch association and story PR flows running against both legacy and FastMCP adapters.

### Slice 5. Azure Execute Primitives

Goal:
Move atomic Azure mutations while keeping authorization logic outside MCP.

FastMCP scope:
- branch association execute;
- draft PR create;
- story status update;
- other isolated Azure mutations that do not require workflow policy inside the tool.

LangGraph scope:
- decide whether execute may run now;
- revalidate preview and approval context before calling the primitive.

Risks:
- callers bypassing LangGraph and treating the primitive as self-authorizing;
- accidental coupling between execute handlers and board rules.

Validation:
- integration tests with fake Azure clients;
- mutable-flow tests proving the graph remains the approval source of truth.

### Slice 6. Adapter Switch in LangGraph

Goal:
Make `kwikledgers_langgraph/` transport-agnostic at runtime.

FastMCP scope:
- expose a stable capability surface equivalent to the legacy adapters.

LangGraph scope:
- add backend selection for `legacy` and `fastmcp`;
- preserve existing ports and state contracts.

Risks:
- leaking transport-specific assumptions into the graphs;
- broad regression surface across existing flows.

Validation:
- run the same graph suite against both backends.

### Slice 7. Windows FastMCP

Goal:
Migrate the environment-sensitive Windows bridge last.

FastMCP scope:
- notification transport;
- Outlook reads and writes;
- reminder scheduling.

LangGraph scope:
- continue treating Windows actions as optional or blocking depending on the flow.

Risks:
- WSL, PowerShell, and COM instability masking transport issues;
- low architectural value compared to Azure and tracking.

Validation:
- smoke tests in supported host environments;
- timeout and error-surface regression tests.

### Slice 8. Legacy Sunset

Goal:
Remove the old MCP runtime only after bounded parallel operation.

FastMCP scope:
- become the default transport layer.

LangGraph scope:
- default adapters to FastMCP;
- preserve rollback to legacy only during the deprecation window.

Risks:
- supporting two backends for too long;
- operational ambiguity during incidents.

Validation:
- bounded rollout checklist with explicit exit criteria for legacy removal.

## Rules That Must Move to LangGraph

- preview-before-mutation enforcement;
- binding between preview, `request_id`, and `active_run_id`;
- resumable approval replay;
- canonical story status flow validation;
- branch-association-before-PR sequencing;
- PR-open-then-status-transition sequencing;
- human approval checkpoints;
- story-start operational order;
- close-preview calculation flow with worked-hours approval;
- coordination between Azure mutation, tracking write, and Windows notification.

## Rules That May Stay in FastMCP

- schema validation;
- payload normalization;
- Azure-specific artifact URI generation;
- deterministic pull-request payload assembly;
- deterministic branch-link preview assembly;
- file persistence for `AI_Tracking`;
- PowerShell bridge execution and timeout handling.

## Domain Impact Summary

### Azure DevOps

Impact:
Highest risk and highest payoff. The current Azure server is the largest concentration of transport, payload, and policy.

### Local Tracking

Impact:
Low-to-medium risk. Good early candidate after Azure reads because it is comparatively self-contained.

### Windows

Impact:
Low architectural priority and high environment sensitivity. Migrate last.

## Validation Strategy

1. Freeze payload fixtures before migration.
2. Compare legacy and FastMCP outputs in shadow mode for read paths.
3. Run focused contract tests for every migrated tool.
4. Run LangGraph graph suites against both backends.
5. Use fake clients for mutation tests before any real-environment smoke checks.
6. Keep feature flags per domain and per capability until rollout is complete.