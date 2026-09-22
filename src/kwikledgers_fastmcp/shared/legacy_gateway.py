from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from kwikledgers_fastmcp.shared.execution_capability import verify_execution_capability
from kwikledgers_fastmcp.shared.legacy_runtime import ensure_legacy_import_path


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
)

WINDOWS_CAPABILITIES = (
    CapabilityDefinition("send_notification", "windows", "execute", "Envia uma notificação ao Windows."),
    CapabilityDefinition("create_calendar_event", "windows", "execute", "Cria evento no calendário."),
    CapabilityDefinition("get_upcoming_deadlines", "windows", "read", "Lê próximos prazos do calendário."),
    CapabilityDefinition("schedule_reminder", "windows", "execute", "Agenda um lembrete."),
)

CAPABILITY_CATALOG = AZURE_CAPABILITIES + TRACKING_CAPABILITIES + WINDOWS_CAPABILITIES


def _server_module_for(domain: str) -> Any:
    ensure_legacy_import_path()
    if domain == "azure_devops":
        from azure_devops import server as legacy_server
    elif domain == "local_tracking":
        from local_tracking import server as legacy_server
    elif domain == "windows":
        from windows_calendar import server as legacy_server
    else:
        raise ValueError(f"Domínio FastMCP desconhecido: {domain}")
    return legacy_server


def _validate_atomic_request(arguments: dict[str, Any]) -> None:
    forbidden = sorted(set(arguments).intersection(FORBIDDEN_WORKFLOW_FIELDS))
    if forbidden:
        raise ValueError(
            "Campos de workflow não são aceitos por capabilities FastMCP: "
            + ", ".join(forbidden)
        )


async def invoke_legacy_capability(
    definition: CapabilityDefinition,
    arguments: dict[str, Any] | None = None,
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
    legacy_server = _server_module_for(definition.domain)
    result = await legacy_server._dispatch(definition.name, normalized_arguments)
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


def invoke_legacy_capability_sync(
    definition: CapabilityDefinition,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return asyncio.run(invoke_legacy_capability(definition, arguments))