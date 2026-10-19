# Alterações de 2026-10-19

## Bundle TLS completo no consumidor da API Azure

- **Resumo:** a configuração do cliente Azure passa a priorizar o bundle completo
  do sistema mesmo quando o processo herda certificados isolados existentes.
- **Arquivos:** `src/kwikledgers_fastmcp/domains/azure_devops/configuration.py`,
  `tests/test_azure_native_configuration.py`, `README.md` e este histórico.
- **Motivo:** a consulta real de histórias reproduziu
  `CERTIFICATE_VERIFY_FAILED` porque a configuração anterior preservava arquivos
  isolados do Zscaler apenas por existirem. A correção fica no consumidor da API,
  sem depender de exports manuais ou do shell usado para iniciar o processo.
- **Compatibilidade:** plataformas sem o bundle Linux mantêm sua configuração;
  bundle vazio ou ilegível falha explicitamente. A verificação TLS não é desativada.
- **Validação:** 81 testes focados do domínio Azure aprovados. Consulta autenticada
  via CLI oficial LangGraph concluída com `status=completed`, sem prefixos TLS no
  comando e com o ambiente herdado original.
