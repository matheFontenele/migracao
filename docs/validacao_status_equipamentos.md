# Validação do status dos equipamentos — legado × refatorado

Data da apuração: 20/11/2026
Base: execução completa da esteira (`run_migracao_completa.sh`, 20/11/2026 15:14 → 16:01) + limpeza de duplicidades de requisições aplicada às 16:30, seguida da reexecução da etapa 8 e da etapa 9 (`REQUISICOES OK`).

**Veredito: alinhamento aceito como está.** 97,5 % dos equipamentos batem por tombo/organização; as divergências restantes são exceções de negócio já documentadas ou estados operacionais mais granulares do refatorado, sem erro de migração identificado.

---

## 1. Por que três medições

O legado e o refatorado não têm chave natural comum confiável. Por isso a validação foi feita em três frentes:

| Frente | Chave | Para que serve |
|---|---|---|
| A | (tombo, organização) | Visão de negócio, comparável ao que o operador vê na tela |
| B | `equipments.id` = `aluguel_equipamentos.id` + `MAPA_SITUACAO_STATUS` | Verdade técnica: é a chave que o `AjustesMovimentos` usa |
| C | Veredito da própria etapa 3 | Referência do estado anterior ao delta e às requisições |

A junção por **número de tombo é ambígua**: o legado tem 24.342 linhas extras (90.794 tombos distintos em 115.136 ativos) e o refatorado tem 1.456 `number` repetidos. A frente B (por id) é a única que isola equipamento de equipamento.

---

## 2. Frente A — por (tombo, organização)

| | |
|---|---|
| Grupos no legado | 80.510 |
| Números no refatorado | 82.888 |
| Presentes nos dois lados | 79.885 |
| **Alinhados** | **77.901 (97,5 %)** |
| Divergentes | 1.984 |

Decomposição dos 1.984:

| Categoria | n | Situação |
|---|---|---|
| `CLIENTE_ESTOQUE` | 1.531 | Exceção esperada — aluguel para cliente interno de estoque/base é liberado para locação pela própria migração. Documentado em `ajustes/excecoes_estoque_esperadas.csv`. |
| Relatório oficial da etapa 3 | 5 | Já em `ajustes/divergencias_movimentos_pos_ajuste.csv`. |
| Divergente por id, acionável | 164 | As mesmas 164 da frente B (§3). |
| Divergente por id, com exceção | 41 | 23 `LEGADO_SEM_MOVIMENTO`, 12 `NOVO_MAIS_RECENTE`, 6 `PARQUET`. |
| Artefato do agrupamento por tombo | 243 | **Não** são divergência por id: 187 `ALUGADO → RESERVADO` (reserva mesclada), 50 `ALUGADO → DISPONÍVEL`, 6 outros. |

Os 243 últimos são o custo de agrupar por (tombo, organização): o mesmo tombo aparece em mais de uma linha legada e em mais de um equipamento refatorado, e a comparação por grupo acusa diferença onde, equipamento a equipamento, o status está correto. É por isso que a frente B existe.

Note que os 1.984 da frente A e as 214 da frente B **convergem**: as 164 acionáveis são exatamente as mesmas nos dois métodos.

---

## 3. Frente B — por id do equipamento + rule-set do `AjustesMovimentos`

`equipments.id` = `aluguel_equipamentos.id`, de/para canônico `MAPA_SITUACAO_STATUS` (`ajustes/ajustes_movimentos.py`, linha ~83):

```
1→2  2→4  3→6  4→9  5→5  6→7  7→1  8→9
9→6  10→9 11→1 12→7 13→6 14→8 15→3 16→5
```

| | |
|---|---|
| Equipamentos ativos analisados | 79.874 |
| Linhas com alguma divergência (`df_div`) | 809 |
| **Divergências de status** | **214** |
| — com exceção documentada | 50 |
| — **acionáveis (sem exceção)** | **164** |
| Divergências de movimento | 334 |
| Divergências de cliente | 2 |

Evidência persistida:

- `ajustes/validacao_status_equipamentos.csv` — as 214 linhas, com `motivo_excecao`, `acionavel` e `corrigivel`.
- `ajustes/validacao_status_equipamentos_resumo.csv` — agregação por par legado→novo.

### 3.1 Decomposição das 214

| Legado → novo | total | acionáveis |
|---|---|---|
| ALUGADO → DISPONÍVEL | 58 | 50 |
| LIBERADO → EM ROTA | 27 | 0 |
| ALUGADO → AG. CARREGAMENTO | 26 | 26 |
| EM MANUTENÇÃO → DISPONÍVEL | 18 | 18 |
| ALUGADO → AG. SUBSTITUIÇÃO | 14 | 12 |
| ALUGADO → EM MANUTENÇÃO | 13 | 8 |
| EM MANUTENÇÃO → AG. SUBSTITUIÇÃO | 11 | 11 |
| EM DEVOLUÇÃO → AG. SUBSTITUIÇÃO | 11 | 11 |
| EM DEVOLUÇÃO → DISPONÍVEL | 8 | 8 |
| EM MANUTENÇÃO → EM DEVOLUÇÃO | 5 | 5 |
| LIBERADO → EM MANUTENÇÃO | 3 | 2 |
| LIBERADO → AG. SUBSTITUIÇÃO | 3 | 3 |
| ALUGADO → EM ROTA | 3 | 2 |
| EM MANUTENÇÃO → EM ROTA | 3 | 0 |
| ALUGADO / EM DEVOLUÇÃO / LIBERADO → demais | 11 | 8 |

Motivos de exceção entre as 214: `LEGADO_SEM_MOVIMENTO` 23, `NOVO_MAIS_RECENTE` 12, `PARQUET` 10, `CLIENTE_SEM_MAPA` 3, `CLIENTE_BLOQUEADO` 1, `REVISAO_MANUAL` 1.

---

## 4. Frente C — veredito da etapa 3 e por que 8 → 214

A etapa 3 (`ajustes_movimentos --aplicar`) reportou internamente **92 linhas, sendo 8 de status**, após aplicar 58.548 correções ("antes 58.640 → depois 92").

A diferença para as 214 explica-se pela **ordem da esteira**. A etapa 3 roda **antes** das etapas 5/6/8:

- **Etapas 5/6** — `migracao_aluguel` grava `status_id=2` nos 10.280 tombos do Parquet; `atualizar_status_equipamentos.py` aplica o delta de produção de 04–12/11 (207 updates: 55 `2→1`, 27 `1→11`, 26 `2→10`, 18 `6→1`, 14 `2→13`, 11 `6→13`, 11 `8→13`).
- **Etapa 8** — requisições 2026 criam movimentos e movem estados operacionais.

Logo, o veredito de 8 divergências é legítimo **para o instante em que foi medido**, mas não representa o banco final.

### Alerta operacional

**Não reexecutar `ajustes_movimentos --aplicar` no fim da esteira** para "zerar" as 164. Ele reaplicaria o status do legado por cima dos status vindos da produção (etapa 6), que são mais recentes. A etapa 3 deve continuar onde está.

---

## 5. Causas das 164 acionáveis

1. **Granularidade operacional do refatorado (maioria).** O refatorado tem estados que o legado não possui: EM ROTA (11), AGUARDANDO CARREGAMENTO (10), AGUARDANDO SUBSTITUIÇÃO (13). Eles são derivados de `shipments` e das substituições — não são erro de carga. Exemplos: `LIBERADO → EM ROTA` (27, todos não acionáveis: 22 `LEGADO_SEM_MOVIMENTO`, 4 `NOVO_MAIS_RECENTE`, 1 `CLIENTE_BLOQUEADO`), `ALUGADO → AG. CARREGAMENTO` (26).
2. **Exceções de negócio documentadas.** Clientes de estoque/base, reserva mesclada, tombos protegidos pelo Parquet, cliente bloqueado, revisão manual — 50 linhas (das 214).
3. **Defasagem de ordem da esteira.** Já descrito no item 4: 8 → 214.
4. **Legado também defasado.** 11 equipamentos com `situacao_id=1` (ALUGADO) cujo último movimento no legado é Devolução ou Baixado — o próprio legado está inconsistente nesses casos.

---

## 6. Achados colaterais verificados

| Medida | Resultado |
|---|---|
| Aluguel no legado com `situacao_id=1` e último movimento Devolução/Baixado | 11 (legado inconsistente) |
| "Só no novo" | 3.003 — 2.794 soft-deleted `status_id=9` (retombados, esperado) + 209 ativos |
| "Só no legado" | 804 — situacao 1 = 489, 14 (DEVOLUÇÃO) = 212, 3 = 52, 7 = 51; efeito de `selecionar_descartes_cliente_estoque`, tombos 0 e órgãos fora de `TODOS_ORGAOS_MAPEADOS` (ex.: 1351, com 34.066 linhas) |
| Distribuição do refatorado (ativos, 80.083) | 1→12.057, 2→60.171, 3→591, 6→5.198, 8→1.891, 9→1, 10→37, 11→58, 13→39, 4/5/7 residuais |
| `migracao_aluguel` | gera movimento apenas para tombos existentes no Parquet (10.280 tombos) |
| `revisao_status` | corrige somente RESERVADO (15→3) e EM DEVOLUÇÃO (14→8); ALUGADO é ignorado por design |

---

## 7. Conclusão

- **97,5 % de alinhamento** por tombo/organização (77.901 de 79.885 grupos comparáveis).
- **0,21 % de divergência técnica acionável** por id (164 de 79.874 equipamentos).
- Nenhum erro de migração identificado nas 164: são estados operacionais mais granulares do refatorado, exceções de negócio ou inconsistência do próprio legado.
- Divergências remanescentes **aceitas conscientemente**, sem ação corretiva. Evidência persistida em `ajustes/validacao_status_equipamentos.csv` e `ajustes/validacao_status_equipamentos_resumo.csv`.

> Nota de método: `AjustesMovimentos.comparar()` reescreve `ajustes/excecoes_estoque_esperadas.csv` a cada execução. Para rodar diagnóstico sem alterar o repositório, faça backup do arquivo antes e restaure depois.
