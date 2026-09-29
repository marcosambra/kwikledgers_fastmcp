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