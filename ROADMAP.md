# Roadmap FastMCP e LangGraph

## Estado Atual

As 30 capacidades originais usam serviços FastMCP nativos. O LangGraph é
dono da governança e seleciona `fastmcp` por padrão; `legacy` é rollback
explícito temporário. As etapas iniciais de scaffold, gateway legado e backend
legado padrão são históricas, não descrevem a implementação atual.

O catálogo LangGraph separa 12 leituras, duas prévias, nove ações atômicas
aprovadas e sete rotas obrigatórias de lifecycle. A criação governada de
`UserStory`, `Task` e `Bug` suporta 15 campos nativos sob restrições do host;
sua integração ainda está em andamento.

## Evidência Conhecida

- FastMCP: suíte anterior com 119 testes aprovados.
- LangGraph: execução completa de 348 testes apresentou três falhas de
	expectativa de outcome; lifecycle 81 e atômico 55 passaram localmente
	após ajustes. A suíte final do fluxo principal ainda está pendente.
- Ambos os locks passaram em `poetry check --lock`, após regeneração do
	lock LangGraph pelo fluxo principal.
- Identidade nativa via stdio resolveu Marcos Pereira anteriormente;
	não houve smoke Azure autenticado, calendário Windows real nem escrita Azure real.

Essas evidências não foram reexecutadas nesta atualização documental.

## Etapas e Gates

| Etapa | Estado | Próximo gate |
|---|---|---|
| Implementações nativas dos três domínios | Implementada | Preservar contratos e evidência de regressão. |
| Backend padrão e imports neutros | Adicionados pelo fluxo principal | Confirmar consumidores e suíte final. |
| Segurança de execução | Journals/checkpoints assinados, recibos até 600 s, nonce vinculado à prévia exata, tokens de cinco minutos e ledger estável | Verificar aquisição externa de recibo assinado pelo host. |
| Criação governada | Em integração | Validar restrições reais do processo e integração do host. |
| Bootstrap e workflows de especialistas | Preparados | Executar instalações e pipelines; registrar resultados. |
| Validação externa | Pendente | Smoke Azure de leitura e calendário Windows no host suportado. |
| Janela de observação | Pendente de definição | Definir responsáveis, duração limitada e rollback. |
| Desativação do legado | Não concluída | Aprovar superfícies protegidas e validar retirada. |

## Ordem de Continuação

1. Registrar o resultado final da suíte completa do LangGraph e resolver
	 eventuais falhas antes de declarar regressão verde.
2. Concluir integração da criação governada e validar aprovação externa
	 com recibo real, expiração, vínculo exato à prévia e rejeição de replay.
3. Executar os smokes de ambiente, instalações de especialistas e pipelines
	 preparados, sem substituir evidência externa por mocks.
4. Definir a janela de observação e verificar os consumidores ainda legados.
5. Apresentar escopo de retirada, validação e rollback para aprovação explícita.
6. Desativar somente as superfícies autorizadas e registrar o resultado.

## Limites de Responsabilidade

FastMCP mantém schemas, prévias técnicas, persistência e execução atômica.
LangGraph mantém preview-first, lifecycle, fluxo canônico de status, cálculo
de horas, sequência de branch/PR e tracking. O host autentica a aprovação
humana e autoriza a thread; a verificação real desse caminho segue pendente.

O agente raiz e suas ferramentas, scheduler, `agent/mcp_servers/` e
configurações de projetos não podem ser alterados por inferência a partir
da troca de backend. Exigem aprovação explícita do usuário.

Consulte o [guia de validação e desativação](../docs/validation/fastmcp-langgraph-legacy-sunset.md)
para comandos de reprodução e checklist. Não há declaração de desligamento
completo, percentual de conclusão, commit ou pipeline executado nesta edição.