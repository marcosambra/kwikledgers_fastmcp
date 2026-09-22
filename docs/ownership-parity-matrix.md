# Matriz de Ownership e Paridade MCP

## Regra de Decisão

- **FastMCP** registra tools, valida schemas, normaliza payloads e executa capacidades atômicas dos domínios.
- **LangGraph** é dono de todas as regras de processo, aprovação, replay, lifecycle, sequência e coordenação entre domínios.
- As regras de `AGENTS.md` e `agent/kwikledgers.agent.md` não podem ser embutidas em handlers FastMCP.

## Regras do Agente

| Regra | Dono | Capability FastMCP afetada | Flow LangGraph |
|---|---|---|---|
| Fluxo canônico de status | LangGraph | `get_story_details`, `update_story_status` | `story_status_transition` via `story_status_policy.py` |
| Preview antes de mutação | LangGraph | `preview_*`, `*_execute` | Todos os fluxos mutáveis |
| Aprovação humana, `request_id` e replay | LangGraph | Nenhuma tool recebe campos de aprovação | Todos os fluxos mutáveis |
| Associação de branch antes da PR | LangGraph | `preview_story_branch_association`, `associate_story_branch`, `create_story_pull_request` | `story_branch_association`, `story_pull_request` |
| PR Draft antes de `Aguardando PR` | LangGraph | `create_story_pull_request`, `update_story_status` | `story_pull_request`, `story_status_transition` |
| Fechamento com `ceil()` e preview de horas | LangGraph | `update_work_item_effort`, `update_story_status` | `task_closure` via `task_closure_policy.py` |
| Registro obrigatório no tracking | LangGraph | `sync_daily_tracking`, `append_daily_action_log`, `record_ai_metrics` | Todos os flows de story |
| Notificação obrigatória ou best-effort | LangGraph | `send_notification` | `daily_summary` e flows futuros |

## Paridade de Capabilities

| Domínio | Tools legadas migradas para FastMCP | Tipo | Dono do processo |
|---|---|---|---|
| Azure DevOps | `get_active_user`, `get_my_work_items`, `get_my_blocked_items`, `get_my_daily_summary`, `get_user_stories`, `get_sprint_stories`, `get_sprint_stories_detailed`, `get_story_details`, `get_open_prs` | Read | LangGraph quando fizer parte de flow |
| Azure DevOps | `preview_story_branch_association`, `preview_story_pull_request` | Preview técnico | LangGraph exige preview e decide continuidade |
| Azure DevOps | `associate_story_branch`, `create_story_pull_request`, `create_work_item`, `update_work_item_content`, `update_work_item_effort`, `move_work_item_to_next_sprint`, `update_story_status`, `add_story_comment` | Execute atômico | LangGraph libera somente após guardrails |
| Azure DevOps | `find_similar_technical_debts`, `save_technical_debt_preview_cache` | Busca e persistência técnica | LangGraph aplica regras de dívida técnica |
| Local tracking | `sync_daily_tracking`, `update_task_control`, `append_daily_action_log`, `record_ai_metrics`, `read_tracking_snapshot` | Read e execute local | LangGraph decide quando e como correlacionar |
| Windows | `send_notification`, `create_calendar_event`, `get_upcoming_deadlines`, `schedule_reminder` | Read e execute externo | LangGraph decide momento, urgência e bloqueio |

## Estado da Migração

| Camada | Estado | Observação |
|---|---|---|
| FastMCP transport | Ativo | Registry FastMCP registra as 30 capabilities atômicas. |
| FastMCP domain implementation | Compatibilidade | Gateway delega temporariamente aos dispatchers legados. |
| LangGraph processos | Ativo | Mantém guardrails, replay, status flow e workflows existentes. |
| Migração de serviços puros | Pendente | Próximo passo é remover import direto dos módulos legados por domínio. |
| Sunset do legado | Pendente | Só após paridade de contratos, shadow mode e rollout por capability. |