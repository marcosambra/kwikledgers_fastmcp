# KwikLedgers FastMCP Blueprint

Blueprint directory for the future FastMCP runtime that will replace the transport layer currently concentrated in `agent/mcp_servers/`.

This directory started as a minimal Python scaffold, but it already carries the first real FastMCP capability slice for `daily_summary`: an Azure read-only provider bridge plus the local tracking sync bridge. The package still remains intentionally partial while preview builders, execute primitives, Windows integrations, and transport registration are migrated in later slices.

## Purpose

- create a new root-level home for the future FastMCP runtime;
- keep Azure DevOps, local tracking, and Windows as explicit integration domains;
- preserve compatibility with the preview-first flows already implemented in `kwikledgers_langgraph/`;
- prevent workflow and governance rules from being re-embedded inside MCP handlers.

## Architectural Principle

FastMCP is the integration facade.
LangGraph is the workflow engine.

That means:

- FastMCP exposes capabilities;
- LangGraph decides sequence, approval, replay, and governance.

## What FastMCP Should Own

- MCP transport and tool registration.
- Request and response schemas.
- Environment bootstrap.
- Domain-local payload normalization.
- Audit hooks.
- Deterministic read operations.
- Deterministic preview helpers when they are strictly domain-local.
- Atomic execute primitives.

## What FastMCP Should Not Own

- preview-first approval policy;
- story lifecycle governance;
- branch-type choice flow;
- status-flow validation as a workflow rule;
- sequencing across Azure, local tracking, and Windows;
- human-in-the-loop checkpoints;
- resumable mutable-flow state.

Those responsibilities belong in `kwikledgers_langgraph/`.

## Proposed Directory Shape

```text
kwikledgers_fastmcp/
  README.md
  ROADMAP.md
  src/
    kwikledgers_fastmcp/
      server.py
      domains/
        azure_devops/
          tools_read.py
          tools_preview.py
          tools_execute.py
          services/
          schemas.py
        local_tracking/
          tools.py
          services/
          schemas.py
        windows/
          tools.py
          services/
          schemas.py
      shared/
        audit.py
        contracts.py
        env.py
        errors.py
        serialization.py
      compatibility/
        legacy_payloads.py
        feature_flags.py
  tests/
    contract/
    compatibility/
    smoke/
```

## Domain Responsibilities

### Azure DevOps

FastMCP should expose:

- read-only story, sprint, PR, and user lookup tools;
- branch-association preview primitives;
- pull-request preview primitives;
- atomic execute primitives for branch association, draft PR creation, and status update;
- compatibility payloads for current adapters while the migration is in progress.

LangGraph should own:

- whether a preview is mandatory before execute;
- whether a given status transition is allowed in the canonical flow;
- whether a branch association must happen before a PR flow continues;
- whether a PR should trigger a story status transition;
- whether approval has been granted for the current preview.

### Local Tracking

FastMCP should expose:

- `AI_Tracking` reads;
- sprint snapshot persistence;
- daily log writes;
- metrics writes;
- full-sync helpers.

LangGraph should own:

- when tracking is required inside a workflow;
- how tracking events are correlated to a mutable flow;
- whether tracking is part of preview, execute, or post-execute behavior.

### Windows

FastMCP should expose:

- toast notifications;
- calendar reads;
- reminder scheduling;
- event creation.

LangGraph should own:

- whether a given workflow should notify;
- whether notification failure is blocking or best-effort;
- when notifications happen relative to approvals and mutations.

## Compatibility Seams

- Transport seam: legacy `mcp.server.Server` can coexist with FastMCP during rollout.
- Contract seam: payload shapes stay stable first, then are simplified only after LangGraph adapters switch over.
- Adapter seam: `kwikledgers_langgraph/` can choose `legacy` or `fastmcp` backends.
- Rollout seam: feature flags per domain and per capability.
- Observability seam: keep `AI_Tracking` and audit output consistent enough to compare both runtimes.

## Immediate Non-Goals

- no runtime code migration in this slice;
- no deletion of `agent/mcp_servers/`;
- no reimplementation of mutable flows inside MCP;
- no change to the current approval-first operational contract.

## Next Artifact

See `ROADMAP.md` in this directory for the recommended migration order, slices, risks, and validation strategy.

## Current Scaffold Status

- `pyproject.toml` defines the standalone package boundary.
- `src/kwikledgers_fastmcp/server.py` now exposes the first real capability seam for `daily_summary` as a legacy-compatible Azure read-only provider.
- `src/kwikledgers_fastmcp/shared/contracts.py` starts the shared contract layer.
- `tests/test_server.py` validates the metadata and the first FastMCP read-only provider bridge.

The `daily_summary` slice now covers two FastMCP-owned seams so far:
- Azure read-only provider bridge
- local tracking sync bridge

It now also includes a Windows notification bridge for `daily_summary`, still backed by the current legacy integration while the environment-sensitive migration remains isolated.

Notifications, preview builders, execute primitives, and transport registration remain outside this slice.