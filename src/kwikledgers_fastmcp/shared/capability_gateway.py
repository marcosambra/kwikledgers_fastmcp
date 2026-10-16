from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from kwikledgers_fastmcp.domains.azure_devops import invoke_azure_capability
from kwikledgers_fastmcp.domains.local_tracking.services import invoke_tracking_capability
from kwikledgers_fastmcp.domains.local_tracking.request_metrics import RequestMetricsService, _identifier
from kwikledgers_fastmcp.domains.windows.services import invoke_windows_capability
from kwikledgers_fastmcp.shared.execution_capability import verify_execution_capability


FORBIDDEN_WORKFLOW_FIELDS = {
    "approved",
    "approval_confirmation",
    "approval_simulation_requested",
    "active_run_id",
    "pending_approval",
    "request_id",
    "run_id",
}


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    domain: str
    operation: str
    description: str


AZURE_CAPABILITIES = (
    CapabilityDefinition("get_active_user", "azure_devops", "read", "Retorna a identidade ativa do Azure DevOps."),
    CapabilityDefinition("get_my_work_items", "azure_devops", "read", "Lista os work items atribuídos ao usuário ativo."),
    CapabilityDefinition("get_my_blocked_items", "azure_devops", "read", "Lista os itens bloqueados atribuídos ao usuário ativo."),
    CapabilityDefinition("get_my_daily_summary", "azure_devops", "read", "Retorna o resumo diário normalizado do Azure DevOps."),
    CapabilityDefinition("get_user_stories", "azure_devops", "read", "Lista histórias atribuídas a um usuário."),
    CapabilityDefinition("get_sprint_stories", "azure_devops", "read", "Lista histórias do sprint atual."),
    CapabilityDefinition("get_sprint_stories_detailed", "azure_devops", "read", "Lista histórias detalhadas do sprint atual."),
    CapabilityDefinition("get_story_details", "azure_devops", "read", "Retorna detalhes de uma história."),
    CapabilityDefinition("get_open_prs", "azure_devops", "read", "Lista PRs abertas de um usuário."),
    CapabilityDefinition("preview_story_branch_association", "azure_devops", "preview", "Gera preview técnico de associação de branch."),
    CapabilityDefinition("preview_story_pull_request", "azure_devops", "preview", "Gera preview técnico de PR Draft."),
    CapabilityDefinition("associate_story_branch", "azure_devops", "execute", "Executa associação atômica de branch."),
    CapabilityDefinition("create_story_pull_request", "azure_devops", "execute", "Cria uma PR Draft atômica."),
    CapabilityDefinition("create_work_item", "azure_devops", "execute", "Cria um work item atômico."),
    CapabilityDefinition("find_similar_technical_debts", "azure_devops", "read", "Busca débitos técnicos similares."),
    CapabilityDefinition("save_technical_debt_preview_cache", "azure_devops", "execute", "Persiste o cache técnico de preview."),
    CapabilityDefinition("update_work_item_content", "azure_devops", "execute", "Atualiza conteúdo atômico de work item."),
    CapabilityDefinition("update_work_item_effort", "azure_devops", "execute", "Atualiza esforço atômico de work item."),
    CapabilityDefinition("move_work_item_to_next_sprint", "azure_devops", "execute", "Move work item para a próxima sprint."),
    CapabilityDefinition("update_story_status", "azure_devops", "execute", "Atualiza status atômico de história."),
    CapabilityDefinition("add_story_comment", "azure_devops", "execute", "Adiciona comentário atômico à história."),
)

TRACKING_CAPABILITIES = (
    CapabilityDefinition("sync_daily_tracking", "local_tracking", "execute", "Sincroniza os artefatos diários de tracking."),
    CapabilityDefinition("update_task_control", "local_tracking", "execute", "Atualiza o controle do sprint."),
    CapabilityDefinition("append_daily_action_log", "local_tracking", "execute", "Adiciona entrada ao log diário."),
    CapabilityDefinition("record_ai_metrics", "local_tracking", "execute", "Registra métricas operacionais de IA."),
    CapabilityDefinition("read_tracking_snapshot", "local_tracking", "read", "Lê o snapshot atual de tracking."),
    CapabilityDefinition("record_request_event", "local_tracking", "execute", "Persiste um evento operacional validado."),
    CapabilityDefinition("read_request_metrics", "local_tracking", "read", "Lê os últimos eventos operacionais (limite 1..1000)."),
)

WINDOWS_CAPABILITIES = (
    CapabilityDefinition("send_notification", "windows", "execute", "Envia uma notificação ao Windows."),
    CapabilityDefinition("create_calendar_event", "windows", "execute", "Cria evento no calendário."),
    CapabilityDefinition("get_upcoming_deadlines", "windows", "read", "Lê próximos prazos do calendário."),
    CapabilityDefinition("schedule_reminder", "windows", "execute", "Agenda um lembrete."),
)

CAPABILITY_CATALOG = AZURE_CAPABILITIES + TRACKING_CAPABILITIES + WINDOWS_CAPABILITIES


def _validate_atomic_request(arguments: dict[str, Any]) -> None:
    forbidden = sorted(set(arguments).intersection(FORBIDDEN_WORKFLOW_FIELDS))
    if forbidden:
        raise ValueError(
            "Campos de workflow não são aceitos por capabilities FastMCP: "
            + ", ".join(forbidden)
        )


async def invoke_capability(
    definition: CapabilityDefinition,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if definition.name in {"record_request_event", "read_request_metrics"}:
        return await _invoke_capability(definition, arguments, {})
    # Correlation is explicit process context, never inferred from a session/token.
    contextual_request = os.environ.get("KWIKLEDGERS_REQUEST_ID")
    request_id = contextual_request or str(uuid4())
    _identifier(request_id, "KWIKLEDGERS_REQUEST_ID")
    metrics = RequestMetricsService()
    state: dict[str, Any] = {}
    started = perf_counter()
    result = None
    operation_error: BaseException | None = None
    metadata = {
        "capability": definition.name, "domain": definition.domain,
        "operation": definition.operation,
    }
    if not contextual_request:
        try:
            await asyncio.to_thread(
                metrics.append_event, request_id=request_id, source="fastmcp",
                kind="request_started", label=definition.name, status="running",
                details={**metadata, "outcome": "running"},
            )
        except Exception as ledger_error:
            raise CapabilityMetricsError(
                definition, ledger_error, state=state,
                result=None, operation_error=None,
            ) from ledger_error
    try:
        result = await _invoke_capability(definition, arguments, state)
        return result
    except BaseException as error:
        operation_error = error
        raise
    finally:
        duration_ms = max(0.0, (perf_counter() - started) * 1000)
        if isinstance(operation_error, asyncio.CancelledError):
            status, outcome = "interrupted", "interrupted"
        elif operation_error is not None:
            status = "failed" if state.get("dispatched") else "blocked"
            outcome = "handler_returned_invalid_envelope" if state.get("returned") else status
        else:
            status, outcome = "completed", "completed"
        try:
            await asyncio.to_thread(
                metrics.append_event,
                request_id=request_id, source="fastmcp", kind="step",
                label=definition.name, status=status, duration_ms=duration_ms,
                details={**metadata, "outcome": outcome},
            )
            if not contextual_request:
                await asyncio.to_thread(
                    metrics.append_event,
                    request_id=request_id, source="fastmcp", kind="request_finished",
                    label=definition.name, status=status,
                    duration_ms=max(0.0, (perf_counter() - started) * 1000),
                    details={**metadata, "outcome": outcome},
                )
        except Exception as ledger_error:
            raise CapabilityMetricsError(
                definition, ledger_error, state=state,
                result=result, operation_error=operation_error,
            ) from ledger_error


class CapabilityMetricsError(RuntimeError):
    """Ledger failure with explicit downstream outcome; never authorize a retry."""

    def __init__(
        self, definition: CapabilityDefinition, ledger_error: Exception, *,
        state: dict[str, Any], result: dict[str, Any] | None,
        operation_error: BaseException | None,
    ) -> None:
        self.operation_executed = bool(state.get("returned"))
        self.operation_dispatched = bool(state.get("dispatched"))
        self.result = result
        self.operation_error = operation_error
        self.ledger_error = ledger_error
        if self.operation_executed:
            outcome = "A operação JÁ RETORNOU após execução"
        elif self.operation_dispatched:
            outcome = "A operação foi enviada; efeitos externos podem ter sido comprometidos"
        else:
            outcome = "A operação NÃO foi enviada ao domínio"
        super().__init__(
            f"{definition.name}: falha no ledger de métricas ({type(ledger_error).__name__}). "
            f"{outcome}. Não repita escritas automaticamente; reconcilie o resultado."
        )


async def _invoke_capability(
    definition: CapabilityDefinition,
    arguments: dict[str, Any] | None,
    state: dict[str, Any],
) -> dict[str, Any]:
    normalized_arguments = dict(arguments or {})
    execution_capability = normalized_arguments.pop("execution_capability", None)
    _validate_atomic_request(normalized_arguments)
    if definition.operation == "execute":
        verify_execution_capability(
            execution_capability,
            capability_name=definition.name,
            arguments=normalized_arguments,
        )
    handlers = {
        "azure_devops": invoke_azure_capability,
        "local_tracking": invoke_tracking_capability,
        "windows": invoke_windows_capability,
    }
    if definition.domain not in handlers:
        raise ValueError(f"Domínio FastMCP desconhecido: {definition.domain}")
    state["dispatched"] = True
    result = await handlers[definition.domain](definition.name, normalized_arguments)
    state["returned"] = True
    if isinstance(result, dict) and {
        "capability", "domain", "operation", "data",
    }.issubset(result):
        if result["capability"] != definition.name or result["domain"] != definition.domain:
            raise ValueError("A capability nativa retornou um envelope inconsistente.")
        if not isinstance(result["data"], dict):
            raise ValueError("A capability nativa retornou dados inválidos.")
        return result
    if isinstance(result, str):
        try:
            data: Any = json.loads(result)
        except json.JSONDecodeError:
            data = {"message": result}
    else:
        data = result
    return {
        "capability": definition.name,
        "domain": definition.domain,
        "operation": definition.operation,
        "data": data,
    }


def invoke_capability_sync(
    definition: CapabilityDefinition,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return asyncio.run(invoke_capability(definition, arguments))