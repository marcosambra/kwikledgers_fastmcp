# Roadmap FastMCP e LangGraph

## Estado Atual

As 30 capacidades originais usam serviços FastMCP nativos. O LangGraph é
dono da governança e aceita somente o backend `fastmcp`; `legacy` foi removido,
não é uma opção de rollback. As etapas iniciais de scaffold e gateway legado
são históricas, não descrevem a implementação atual.

O catálogo LangGraph separa 12 leituras, duas prévias, nove ações atômicas
aprovadas e sete rotas obrigatórias de lifecycle. A criação governada de
`UserStory`, `Task` e `Bug` suporta 15 campos nativos sob restrições do host;
a integração local passou, mas as restrições reais do host continuam pendentes.

## Evidência Conhecida

- Consolidação P0–P3: FastMCP com 203 testes e LangGraph com 461 testes
	aprovados na rodada completa anterior, registrada no histórico do workspace.
- Ambos os locks passaram em `poetry check --lock`, após regeneração do
	lock LangGraph pelo fluxo principal.
- Retomada operacional: leitura Azure autenticada, resumo diário, refresh
	compilado da extensão, métricas compartilhadas, calendário Outlook e smoke
	de ativação/comandos/restauração em Extension Host real aprovados.
- Nenhuma escrita Azure ou troca de agendamento foi realizada.

As suítes completas anteriores não foram reexecutadas nesta atualização
documental. As revisões e os comandos dos smokes reais estão no
[guia operacional](../docs/validation/fastmcp-langgraph-legacy-sunset.md).

## Etapas e Gates

| Etapa | Estado | Próximo gate |
|---|---|---|
| Implementações nativas dos três domínios | Implementada | Preservar contratos e evidência de regressão. |
| Backend e imports neutros | Somente FastMCP; backend legado removido | Migrar os dois agendamentos legados após aprovação. |
| Segurança de execução | Aprovação local explícita após prévia; tokens `.local-v1` de até cinco minutos, vínculo exato e anti-replay; recibos antigos rejeitados | Preservar a fronteira de confiança local e a revisão nativa. |
| Criação governada | Integração local aprovada | Obter configuração confiável com restrições reais do processo. |
| Bootstrap e workflows de especialistas | Preparados | Executar instalações e pipelines; registrar resultados. |
| Validação externa de leitura | Azure e calendário aprovados | Preservar evidências; não inferir autorização de mutação. |
| Janela de observação | Pendente de definição | Definir responsáveis, duração limitada e rollback. |
| Desativação do legado | Não concluída | Aprovar superfícies protegidas e validar retirada. |

## Ordem de Continuação

1. Aprovar as prévias exatas da tarefa Windows e do cron ainda legados,
	 exportar suas configurações e trocar somente as ações autorizadas.
2. Validar a configuração real de criação fornecida pelo host; não fabricar
	 restrições nem usar APIs internas como caminho alternativo.
3. Executar instalações de especialistas e pipelines preparados somente
	 com escopo e autorização próprios; registrar resultados.
4. Definir janela, responsável e rollback; observar a execução agendada real.

## Limites de Responsabilidade

FastMCP mantém schemas, prévias técnicas, persistência e execução atômica.
LangGraph mantém preview-first, lifecycle, fluxo canônico de status, cálculo
de horas, sequência de branch/PR e tracking. O host coleta a confirmação humana
explícita e retoma o pedido correspondente; isso não comprova identidade externa.

O agente raiz e suas ferramentas, scheduler, `agent/mcp_servers/` e
configurações de projetos não podem ser alterados por inferência a partir
da troca de backend. Exigem aprovação explícita do usuário.

Consulte o [guia de validação e desativação](../docs/validation/fastmcp-langgraph-legacy-sunset.md)
para comandos de reprodução e checklist. Não há declaração de desligamento
completo, percentual de conclusão, commit ou pipeline executado nesta edição.