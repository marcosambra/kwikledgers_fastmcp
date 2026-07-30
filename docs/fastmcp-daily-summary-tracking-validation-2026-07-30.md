# FastMCP Daily Summary Tracking Validation - 2026-07-30

Slice: migracao do `TrackingSyncPort` de `daily_summary` para `kwikledgers_fastmcp`.

Validacoes executadas:

- `poetry install`
- `poetry run python -m unittest discover -s tests`
- `cd ../kwikledgers_langgraph && poetry install`
- `cd ../kwikledgers_langgraph && poetry run kwikledgers-langgraph --help`
- `cd ../kwikledgers_langgraph && poetry run python -m unittest tests.test_backend_factory tests.test_daily_summary_cli`

Resultado:

- o pacote `kwikledgers-fastmcp` executou `4` testes com sucesso
- o `kwikledgers_langgraph` manteve `12` testes focados verdes no wiring do selector e da CLI principal
- o fluxo `daily_summary` ficou hibrido apenas na notificacao, nao mais no tracking local

Escopo coberto:

- adapter FastMCP para `local_tracking._sync_daily_tracking`
- selector do LangGraph atualizado para usar esse adapter quando `backend=fastmcp`
- notificacoes ainda preservadas no legado por decisao explicita do slice