from __future__ import annotations

from typing import Any


def format_type_breakdown(type_breakdown: Any) -> str:
    if not isinstance(type_breakdown, dict) or not type_breakdown:
        return "nenhum tipo contabilizado"
    return ", ".join(
        f"{name}={count}" for name, count in sorted(type_breakdown.items(), key=lambda entry: str(entry[0]).lower())
    )


def format_identity_context(payload: dict[str, Any]) -> str:
    identity_context = payload.get("user_identity_context", {})
    if not isinstance(identity_context, dict):
        return "desconhecida"
    resolved_email = identity_context.get("resolved_email") or payload.get("user_email") or "desconhecido"
    email_source = identity_context.get("email_source") or "origem-desconhecida"
    env_file_path = identity_context.get("env_file_path")
    if env_file_path:
        return f"{resolved_email} via {email_source} ({env_file_path})"
    return f"{resolved_email} via {email_source}"


def build_control_markdown(payload: dict[str, Any], today: str) -> str:
    items = payload.get("items", [])
    blocked_items = payload.get("blocked_items", [])
    returned_items = payload.get("returned_items", [])
    open_prs = payload.get("open_prs", [])
    counts = payload.get("counts", {})
    child_items_summary = payload.get("child_items_summary", {})
    project_progress = payload.get("project_progress", {})
    sprint_stories = payload.get("sprint_stories", [])
    sprint_context = payload.get("sprint_context", {})
    lines = [
        "# Sprint Control Snapshot", "", f"Data: {today}",
        f"Usuario: {payload.get('user_email', 'desconhecido')}",
        f"Origem da identidade: {format_identity_context(payload)}",
        f"Projeto: {payload.get('project', 'desconhecido')}", "",
        "## Contexto do sprint", "",
        f"- Nome: {sprint_context.get('name') or 'desconhecido'}",
        f"- Caminho: {sprint_context.get('path') or 'desconhecido'}",
        f"- Inicio: {sprint_context.get('start_date') or 'nao informado'}",
        f"- Fim: {sprint_context.get('finish_date') or 'nao informado'}",
        f"- Goal: {sprint_context.get('goal') or 'nao exposto pela API atual'}", "",
        "## Resumo", "",
        f"- Itens atribuídos: {counts.get('assigned_items', 0)}",
        f"- Itens bloqueados: {counts.get('blocked_items', 0)}",
        f"- Itens devolvidos ao usuario: {len(returned_items)}",
        f"- User stories do usuario no sprint: {counts.get('user_stories', 0)}",
        f"- Tasks do usuario em todos os status: {counts.get('tasks_all_statuses', 0)}",
        f"- Tasks por status: {format_type_breakdown(counts.get('tasks_by_state', {}))}",
        f"- Itens atribuídos por tipo: {format_type_breakdown(counts.get('assigned_items_by_type', {}))}",
        f"- Children dos itens atribuídos: {child_items_summary.get('total_children', 0)}",
        f"- Children bloqueados dos itens atribuídos: {child_items_summary.get('blocked_children', 0)}",
        f"- Historias totais do sprint: {project_progress.get('total_stories', counts.get('sprint_stories', len(sprint_stories)))}",
        f"- PRs abertas: {counts.get('open_prs', 0)}",
        f"- Story points restantes do projeto: {payload.get('remaining_story_points', 0)}",
        f"- Story points atribuidos aos itens do usuario: {payload.get('assigned_story_points', 0)}",
        f"- Horas restantes estimadas: {payload.get('remaining_work_hours', 0)}", "",
        "## Progresso do projeto no sprint", "",
        f"- Historias concluidas: {project_progress.get('completed_stories', 0)}",
        f"- Historias ativas: {project_progress.get('active_stories', 0)}",
        f"- Historias bloqueadas: {project_progress.get('blocked_stories', 0)}",
        f"- Historias sem responsavel: {project_progress.get('unassigned_stories', 0)}",
        f"- Historias sem estimativa: {project_progress.get('unestimated_stories', 0)}",
        f"- Historias ativas sem estimativa: {project_progress.get('active_unestimated_stories', 0)}",
        f"- Story points totais: {project_progress.get('total_story_points', 0)}",
        f"- Story points concluidos: {project_progress.get('completed_story_points', 0)}",
        f"- Story points restantes: {project_progress.get('remaining_story_points', payload.get('remaining_story_points', 0))}",
        f"- Progresso por historias: {project_progress.get('progress_percent_by_story_count', 0)}%",
        f"- Progresso por story points: {project_progress.get('progress_percent_by_story_points', 0)}%", "",
    ]
    if project_progress.get("progress_warning"):
        lines.extend(["## Alerta de estimativa", "", f"- {project_progress['progress_warning']}", ""])
    lines.extend(["## Itens atribuídos", ""])
    if items:
        for item in items:
            lines.append(f"- KL-{item.get('id')}: {item.get('title')} | {item.get('work_item_type')} | {item.get('state')} | blocked={item.get('is_blocked')}")
    else:
        lines.append("- Nenhum item atribuído no sprint atual.")
    lines.extend(["", "## Itens bloqueados", ""])
    if blocked_items:
        for item in blocked_items:
            lines.append(f"- KL-{item.get('id')}: {item.get('title')} | {item.get('state')}")
    else:
        lines.append("- Nenhum item bloqueado.")
    lines.extend(["", "## Children dos itens atribuídos", ""])
    if child_items_summary.get("total_children", 0):
        lines.append(
            f"- Totais: total={child_items_summary.get('total_children', 0)} | abertas={child_items_summary.get('open_children', 0)} | bloqueadas={child_items_summary.get('blocked_children', 0)} | concluidas={child_items_summary.get('completed_children', 0)}"
        )
        lines.append(f"- Por tipo: {format_type_breakdown(child_items_summary.get('children_by_type', {}))}")
        for parent in child_items_summary.get("children_by_parent", []):
            lines.append(
                f"- Pai KL-{parent.get('parent_id')}: {parent.get('parent_title')} | children={parent.get('child_count')} | abertas={parent.get('open_child_count')} | bloqueadas={parent.get('blocked_child_count')}"
            )
    else:
        lines.append("- Nenhum child encontrado para os itens atribuídos no snapshot atual.")
    lines.extend(["", "## Itens devolvidos ao usuario", ""])
    if returned_items:
        for item in returned_items:
            lines.append(f"- KL-{item.get('id')}: {item.get('title')} | {item.get('previous_state')} -> {item.get('current_state')}")
    else:
        lines.append("- Nenhum item devolvido detectado desde o ultimo snapshot.")
    lines.extend(["", "## PRs abertas", ""])
    if open_prs:
        for pull_request in open_prs:
            lines.append(f"- PR #{pull_request.get('pull_request_id')}: {pull_request.get('title')} | {pull_request.get('repository')} | {pull_request.get('age_days')} dias")
    else:
        lines.append("- Nenhuma PR aberta.")
    lines.extend(["", "## Historias do sprint", ""])
    if sprint_stories:
        for story in sprint_stories:
            lines.append(
                f"- KL-{story.get('id')}: {story.get('title')} | {story.get('state')} | "
                f"points={story.get('story_points')} | responsavel={story.get('assigned_to')} | blocked={story.get('is_blocked')}"
            )
    else:
        lines.append("- Nenhuma historia retornada para o sprint atual.")
    return "\n".join(lines) + "\n"


def build_default_log_details(payload: dict[str, Any]) -> str:
    counts = payload.get("counts", {})
    returned_items = payload.get("returned_items", [])
    children = payload.get("child_items_summary", {})
    progress = payload.get("project_progress", {})
    sprint_context = payload.get("sprint_context", {})
    lines = [
        f"Resumo do sprint para {payload.get('user_email', 'desconhecido')}.",
        f"Sprint atual: {sprint_context.get('name') or 'desconhecido'}.",
        f"Goal do sprint: {sprint_context.get('goal') or 'nao exposto pela API atual'}.",
        f"Itens atribuídos: {counts.get('assigned_items', 0)}.",
        f"Breakdown por tipo dos itens atribuídos: {format_type_breakdown(counts.get('assigned_items_by_type', {}))}.",
        f"Tasks do usuario em todos os status: {counts.get('tasks_all_statuses', 0)}, com breakdown {format_type_breakdown(counts.get('tasks_by_state', {}))}.",
        f"Itens bloqueados: {counts.get('blocked_items', 0)}.",
        f"Historias bloqueadas no sprint: {progress.get('blocked_stories', 0)}, somando {progress.get('blocked_story_points', 0)} story points bloqueados.",
        f"Children ligados aos itens atribuídos: {children.get('total_children', 0)}, com {children.get('open_children', 0)} abertos e {children.get('blocked_children', 0)} bloqueados.",
        f"Itens devolvidos detectados: {len(returned_items)}.",
        f"Historias totais do sprint: {progress.get('total_stories', counts.get('sprint_stories', 0))}.",
        f"Story points restantes do projeto: {payload.get('remaining_story_points', 0)}.",
        f"Progresso do projeto: {progress.get('progress_percent_by_story_count', 0)}% por historias e {progress.get('progress_percent_by_story_points', 0)}% por story points.",
        f"PRs abertas: {counts.get('open_prs', 0)}.",
        f"Identidade resolvida: {format_identity_context(payload)}.",
    ]
    parents = children.get("children_by_parent", [])
    if isinstance(parents, list) and parents:
        parent = parents[0]
        if parent.get("blocked_child_count", 0) > 0:
            lines.append(
                f"Maior foco em children: KL-{parent.get('parent_id')} tem "
                f"{parent.get('blocked_child_count', 0)} children bloqueados e {parent.get('open_child_count', 0)} abertos."
            )
        elif parent.get("open_child_count", 0) > 0:
            lines.append(f"Maior carga em children: KL-{parent.get('parent_id')} tem {parent.get('open_child_count', 0)} children abertos.")
    if progress.get("progress_warning"):
        lines.append(progress["progress_warning"])
    return "\n".join(lines)