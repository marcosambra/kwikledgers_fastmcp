# KwikLedgers FastMCP Blueprint

Blueprint directory for the future FastMCP runtime that will replace the transport layer currently concentrated in `agent/mcp_servers/`.

This directory now exposes the FastMCP capability transport for Azure DevOps, local tracking, and Windows. The active runtime path remains in `kwikledgers_langgraph/`, which owns workflow and governance while FastMCP owns atomic integration capabilities.

## Purpose

- create a new root-level home for the future FastMCP runtime;
- keep Azure DevOps, local tracking, and Windows as explicit integration domains;
- preserve compatibility with the preview-first flows already implemented in `kwikledgers_langgraph/`;
- prevent workflow and governance rules from being re-embedded inside MCP handlers.

## Architectural Principle

FastMCP is the integration facade.
LangGraph is the workflow engine.

Operational correlation uses version `1` of `kwikledgers/context` in MCP
`_meta`, separate from business arguments and execution capabilities.
`request_id` is required; workspace, session, run and parent/child span IDs are
optional safe identifiers. The server binds metadata to the individual async
request, not the process environment, so concurrent HTTP callers cannot share
another caller's context. Metadata never authorizes an operation.

`domains.local_tracking.handoffs.HandoffStore` provides atomic, revisioned
storage for local continuation requests using the existing tracking lock.
LangGraph owns acceptance, session binding and lifecycle transitions. This is
not an Azure execution capability or a replacement for human approval.

Daily summaries include `original_estimate_hours`, `completed_work_hours` and
`remaining_work_hours` for assigned items and linked children. Missing effort
fields remain null rather than implying zero remaining work. Story percentages
and charts are computed by the consumer, not by FastMCP workflows.

`get_story_details` includes a read-only `hierarchy_context`, following parent
links from the requested task or story through Feature and Epic. Ancestors
retain title, state, description, acceptance criteria and revision. Missing
levels, invalid or ambiguous links, cycles and unavailable ancestors are
reported explicitly; `complete` does not imply that missing product content
has been inferred. Traversal is limited to 16 ancestors and never changes links.

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

- Contract seam: payload shapes stay stable first, then are simplified only after LangGraph adapters switch over.
- Adapter seam: `kwikledgers_langgraph/` consumes this server as its only backend.
- Rollout seam: feature flags per domain and per capability.

## Current Migration Boundary

- no reimplementation of mutable flows inside MCP;
- no change to the current approval-first operational contract.
- no workflow rule, approval policy, or resumable state inside FastMCP handlers.

## Next Artifact

See `ROADMAP.md` in this directory for the recommended migration order, slices, risks, and validation strategy.

## Current Capability Status

- `pyproject.toml` defines the standalone package boundary.
- `src/kwikledgers_fastmcp/server.py` now exposes a real FastMCP tool registry for the Azure DevOps, local tracking, and Windows capability catalog.
- `src/kwikledgers_fastmcp/shared/contracts.py` starts the shared contract layer.
- `tests/test_server.py` validates o registry, o envelope de capabilities e o dispatcher nativo.

Todas as capabilities registradas são atendidas por serviços FastMCP nativos. O
LangGraph usa `fastmcp` como único backend.

The ownership and parity map is maintained in [docs/ownership-parity-matrix.md](docs/ownership-parity-matrix.md).

## Trilha operacional por solicitação

`kwikledgers_fastmcp.domains.local_tracking.request_metrics.RequestMetricsService`
é o contrato compartilhado de persistência, sem dependências de hooks, grafos ou
extensão. A fonte única é `AI_Tracking/Metrics/request-events.jsonl`. O serviço
reutiliza o lock de threads e processos do `TrackingService`, sem novos locks
concorrentes ou índices de estado secundários.

```python
from pathlib import Path
from kwikledgers_fastmcp.domains.local_tracking.request_metrics import RequestMetricsService

metrics = RequestMetricsService(root=Path("/workspace"))
started = metrics.start_request("session-123", label="Validação operacional")
metrics.append_event(
    request_id=started["request_id"], session_id="session-123",
    source="agent", kind="step", label="Executar testes", status="completed",
    duration_ms=123.4, details={"measurement": "elapsed_monotonic"},
)
metrics.finish_request("session-123")
events = metrics.read_events(limit=100)
```

### API e armazenamento

- `RequestMetricsService(root: Path | None = None, *, tracking_root: Path | None = None)`:
  `root` é o workspace e resulta em `root/AI_Tracking`. `tracking_root` é uma
  pasta de tracking direta, com qualquer nome, e tem precedência. Sem ambos,
  `KWIKLEDGERS_AI_TRACKING_ROOT` define a pasta direta; sem essa variável,
  vale o workspace descoberto por `TrackingService`/`KWIKLEDGERS_ROOT_DIR`.
  Um `root` explícito não é redirecionado pela variável de tracking.
- `start_request(session_id: str, label: str = "Solicitação do usuário") -> dict`:
  retorna o evento inicial com um novo `request_id` UUID. Interrompe primeiro
  a solicitação ainda aberta **da mesma sessão**, sem alterar outras sessões.
- `append_event(*, request_id: str, session_id: str = "", source: str, kind: str,
  label: str, status: str, duration_ms: float | None = None,
  details: dict | None = None, event_id: str | None = None) -> dict`:
  aceita também ciclos independentes de grafos, sem sessão artificial.
  `event_id` opcional é um UUID determinístico, por exemplo `uuid5` baseado em
  request/run/índice/timestamp observado. Sob lock, uma repetição com o mesmo
  conteúdo retorna o evento original, preservando `at`; conteúdo divergente
  com o mesmo ID é rejeitado. A busca é linear no ledger, sem índice secundário.
- `current_request(session_id: str) -> dict | None`: deriva o pedido aberto
  exclusivamente do ledger; não há cache em memória como fonte de estado.
- `finish_request(session_id: str, status: str = "completed") -> dict | None`:
  encerra o pedido aberto. Repetir o fechamento não grava outro evento.
- `read_events(limit: int = 100) -> list[dict]`: lê as últimas 1 a 1000 linhas,
  em ordem cronológica, varrendo o final do arquivo com memória limitada.
- `TrackingService(root).locked()` expõe o mesmo lock para integrações locais
  que precisem proteger outros arquivos; não chamar métodos de métricas dentro
  desse contexto, pois já adquirem o lock.

Cada linha tem `schema_version: 1`, `event_id` UUID, `request_id`, `session_id`
(vazio se desconhecido), `source`, `kind`, `at` ISO UTC, `label`, `status`,
`details` e `duration_ms` opcional. IDs de correlação/sessão admitem somente
letras ASCII, números e `_.:-`, com até 128 caracteres. Fontes:
`agent`, `fastmcp`, `langgraph`. Tipos: `request_started`, `step`,
`request_finished`. Status: `running`, `completed`, `failed`, `blocked`,
`interrupted`; início exige `running` e término exige status terminal.

Início e fechamento medem **tempo de parede UTC**, não CPU nem trabalho ativo.
Uma volta do relógio deixa a duração desconhecida, com motivo explícito.
Durações informadas devem ser números finitos não negativos, nunca booleanos.
O contrato permite duração ausente ou `null` para desconhecido; novas escritas
omitem `duration_ms` quando `None`. Nunca substituir desconhecido por zero.
Ferramentas/grafos devem descrever sua medição em `details.measurement`.
`at` é o instante de emissão; `details.observed_at` pode preservar o instante
original de um evento exportado.

### Privacidade e capabilities

`label` e valores textuais de `details` são resumos operacionais limitados a
160 caracteres, com controles removidos e redação defensiva de URLs, tokens e
atribuições comuns de credenciais. Isso não é um classificador completo de
segredos: os chamadores **não devem enviar prompts, argumentos, saídas,
mensagens brutas de exceções ou segredos**, nem colocá-los em IDs.
Campos de `details` não permitidos são descartados; valores aninhados não são
aceitos. Allowlist: `capability`, `domain`, `operation`, `outcome`, `flow_name`,
`node`, `correlation_id`, `run_id`, `parent_request_id`, `observed_at`,
`measurement`, `tool_name`, `tool_call_id`, `reason`, `evidence`, `error_type`,
`scope` (strings) e `error` (booleano).

- `record_request_event`: capability `execute`, protegida pelo token existente,
  recebe `{"event": {...campos de append_event...}}`. `request_id` continua
  proibido no nível superior de argumentos de workflow.
- `read_request_metrics`: capability `read`, recebe `{"limit": 100}` e retorna
  `data.events`. As duas capabilities são excluídas da autoinstrumentação.
- As demais chamadas no gateway medem duração com `perf_counter` e persistem
  somente `capability/domain/operation/outcome`, nunca argumentos ou retornos.
  `KWIKLEDGERS_REQUEST_ID` correlaciona explicitamente a chamada; nenhum ID de
  sessão é inferido de tokens, pedidos abertos ou argumentos. Sem essa variável,
  a chamada recebe UUID próprio e ciclo `request_started → step → request_finished`.
  Em contexto explícito, o dono da solicitação é responsável pelo fechamento.

### Falhas e limites

Os gates de workflow e consumo de token continuam antes do dispatch. Um erro
no ledger não é ignorado. `CapabilityMetricsError` informa
`operation_dispatched`, `operation_executed`, `result`, `operation_error` e
`ledger_error`. Se o domínio já retornou, o resultado está disponível em
`result`; se lançou exceção após dispatch, efeitos externos são **incertos**.
Isso vale especialmente para Azure: **não repetir escrita automaticamente**;
reconciliar o efeito externo e a autorização antes de qualquer nova tentativa.
Falha ao gravar o início standalone bloqueia o dispatch.

Append usa flush/fsync sob lock. Falhas de disco podem ocorrer depois de bytes
terem sido escritos; o evento pode existir apesar do erro. Linha parcial é
reportada e não é corrigida ou descartada automaticamente. `event_id`
determinístico permite reconciliar exportações repetidas. Interrupção entre
o encerramento anterior e um novo início pode deixar apenas o encerramento.
Não há rotação, retenção automática nem transação com Azure; scans de estado
e deduplicação crescem com o ledger. O lock requer filesystem com suporte a
`flock` (Linux) ou `msvcrt.locking` (Windows).

### Evidência desta alteração — 2026-10-15

- Arquivos: novo `domains/local_tracking/request_metrics.py`, integração no
  `domains/local_tracking/services.py` e `shared/capability_gateway.py`,
  `tests/test_request_metrics.py`, isolamento da instrumentação nos testes
  existentes de gateway em `tests/test_server.py` e este README.
- Motivo: trilha única por solicitação com duração observada, correlação
  explícita e privacidade, pronta para consumidores independentes.
- `tests.test_request_metrics`: 26 testes aprovados, incluindo concorrência
  multiprocessos, sessões distintas, deduplicação concorrente, roots arbitrários,
  duração desconhecida, sanitização, gates, cancelamento e erro de ledger
  posterior à execução.
- Suíte FastMCP: 194 testes, com 1 falha e 2 erros preexistentes reproduzidos
  individualmente em uma cópia limpa de `HEAD`:
  `test_root_discovery_without_legacy_agent`,
  `test_execute_capability_allows_signed_matching_payload` e
  `test_storage_failures_and_nonpersistent_paths_fail_closed`.
  A primeira reprodução depende de manter os diretórios de teste dentro do
  workspace; as duas outras pertencem ao contrato anterior de capabilities.
  Não foram alteradas nesta entrega. Nenhuma escrita Azure, commit ou push.