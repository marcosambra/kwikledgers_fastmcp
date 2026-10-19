# Matriz de Ownership e Paridade MCP

## Regra de Decisão

- **FastMCP** registra tools, valida schemas, normaliza payloads e executa capacidades atômicas dos domínios.
- **LangGraph** é dono de todas as regras de processo, aprovação, replay, lifecycle, sequência e coordenação entre domínios.
- As regras de `.github/copilot-instructions.md` e os padrões de `agent/kwikledgers.agent.md` não podem ser embutidos em handlers FastMCP; `AGENTS.md` contém somente o mapa.
- Toda capability FastMCP de tipo `execute` exige token local `.local-v1`, emitido pelo LangGraph após os guardrails. O FastMCP valida ação, digest, run, request, TTL de até cinco minutos e uso único no ledger compartilhado. Tokens locais não são aceitos por HTTP; recibos antigos são rejeitados.

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
| FastMCP execute authorization | Ativo | Tools `execute` exigem token `.local-v1` emitido pelo LangGraph com ação, digest, run, request, expiração e anti-replay. |
| FastMCP domain implementation | Ativo | As 30 capabilities usam serviços nativos de Azure DevOps, tracking local e Windows. |
| LangGraph processos | Ativo | Mantém guardrails, replay, status flow e workflows existentes. |
| Migração de serviços puros | Concluída | A configuração usa arquivo explícito ou `.env` do workspace; não consulta `agent/mcp_servers`. |
| Sunset do legado | Agendamentos conhecidos migrados; observação pendente | Somente FastMCP é suportado. Comandos da tarefa Windows e do cron migrados após aprovação em 2026-10-19, com configurações preservadas e smokes executados. Próximos gatilhos naturais ainda não observados. |

## Evidência do Slice de Identidade Ativa (2026-09-25)

- `get_active_user` preserva o envelope FastMCP e a resposta textual do legado; testes cobrem identidade configurada, fallback do `.env`, Git e e-mail ausente, sem chamada ao `_dispatch`.
- Validação: `poetry run python -m unittest discover -s tests -p 'test_*.py'` no diretório `kwikledgers_fastmcp` (16 testes, OK).
- Compatibilidade: as demais capabilities seguem no gateway legado. Nenhum fluxo mutável, política LangGraph ou autorização foi alterado.

O parágrafo de compatibilidade acima descreve somente o slice histórico de
2026-09-25. O estado atual está na tabela de migração: não existe gateway ou
backend legado suportado. Comandos, revisões e limites da validação real estão
no [guia operacional](../../docs/validation/fastmcp-langgraph-legacy-sunset.md).