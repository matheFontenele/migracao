# Ajustes pós-migração

## Aluguel avulso CO Brasil

Executar a partir da raiz do repositório:

```bash
python ajustes_pos_migracao/aluguel_co_brasil.py
```

O script pergunta o destino: **refatorado local** ou **refatorado produção**. A opção local exige `LOCAL_DB_HOST`, `LOCAL_DB_PORT`, `LOCAL_DB_USER` e `LOCAL_DB_PASS`, sem fallback, e conecta exclusivamente ao banco `controle-interno`. Configure esse perfil para apontar à instância de teste local real. Um endereço `localhost` com porta SSH encaminhada ainda pode chegar à produção; não reutilize o endpoint do túnel no perfil local, a menos que sua intenção seja acessar produção (nesse caso escolha explicitamente a opção de produção). O runner exibe endpoint e identidade retornada pelo servidor (`DATABASE()`, `@@hostname`, `@@port`) antes de qualquer gravação.

Para produção, configure previamente `PROD_DB_HOST`, `PROD_DB_PORT`, `PROD_DB_USER` e `PROD_DB_PASS` no ambiente do terminal/servidor. `PROD_DB_NAME` é opcional e, se informado, precisa ser exatamente `controle-interno`; não há fallback para a conexão local ou legado. Não grave credenciais neste repositório. A função `config.database.obter_engine_producao()` cria essa conexão apenas quando explicitamente chamada.

O script usa `docs/co_brasil.csv`, mostra o resumo e gera uma prévia em `/tmp/opencode/aluguel-co-brasil/preview_aluguel_co_brasil.csv`. A organização da operação é fixada em `1115` para service orders, movements e `movement_items.new_organization_id`, independentemente da organização do cadastro do cliente. A gravação só acontece depois de digitar exatamente `CONFIRMO LOCAL-REFATORADO` ou `CONFIRMO PRODUCAO`, conforme o destino escolhido. Revise endpoint, identidade do servidor e prévia antes de confirmar: a aplicação cria movimentos de aluguel e atualiza equipamentos. Execuções com linhas ignoradas ou erros são bloqueadas. Não conecta nem grava nada apenas ao importar o módulo.
