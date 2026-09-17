# Validação do Registry de Capabilities

Data: 2026-09-17

## Slice

- Registro do catálogo completo de capabilities atômicas de Azure DevOps, local tracking e Windows no FastMCP.
- Fiação dos processos LangGraph de fechamento de task e histórias detalhadas do sprint.

## Evidências

```text
kwikledgers_fastmcp
poetry run python -m unittest discover -s tests
Ran 10 tests
OK

FastMCP registry
30 tools registradas

kwikledgers_langgraph
poetry run python -m unittest tests.test_task_closure_graph tests.test_task_closure_cli tests.test_sprint_stories_graph tests.test_sprint_stories_cli
Ran 22 tests
OK
```

## Observação

O FastMCP expõe capabilities atômicas por compatibilidade temporária com os dispatchers legados. Regras de aprovação, replay, lifecycle e sequência permanecem exclusivamente no LangGraph.