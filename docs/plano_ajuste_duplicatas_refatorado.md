# Plano — Ajuste das duplicatas direto no banco refatorado (`controle-interno`)

Documento de handoff. Espelha `docs/plano_ajuste_duplicatas_legado.md`, mas o alvo
das escritas é o **banco migrado**. Escrito para ser delegado a um agente
implementador.

Base medida: MySQL 8 local `127.0.0.1:3307`, banco `controle-interno`, estado
gerado pela última execução completa do pipeline (`main.py todos`).

---

## 0. A diferença que define o desenho: o refatorado é derivado

O banco `controle-interno` **não é fonte de verdade**. Ele é reconstruído:

- `cadastros/equipamentos/migracao_equipamentos.py:1032` chama
  `executar_truncate_tabelas(engine_new, TABELAS)` com
  `equipments, equipment_history, transaction_items, product_items, products,
  transactions, ...` (`TABELAS`, linha 113);
- `main.py` trunca ainda as tabelas de movimento em `TABELAS_MOVIMENTOS`
  (`movement_items, movements, service_orders, service_order_items,
  equipment_history, maintenances, volumes, volume_items, ...`, linha 222).

Consequência direta: **qualquer `UPDATE` feito à mão no refatorado é apagado na
próxima migração.** O ajuste precisa ser:

1. um **módulo pós-migração** em `ajustes/`, no mesmo padrão de
   `ajustes/limpar_duplicidades_requisicoes.py` (argparse, dry-run default,
   CSV de auditoria, bloqueio por dependentes);
2. **idempotente** — rodar duas vezes não pode decrementar estoque duas vezes
   nem “ressuscitar” nada;
3. **determinístico** — a decisão sai de um `mapa_canonico.csv` versionado no
   repositório, não de heurística recalculada a cada execução (a heurística
   existe só para gerar/revalidar o mapa).

Por isso o módulo entra no pipeline **no fim**: `TAREFAS` (`main.py:240`) e a
lista de `todos` (`main.py:266`), depois de `ajustes_movimentos`. Rodar depois
da migração de movimentos é obrigatório — senão os `movement_items` recém
inseridos voltam a apontar para as cópias que serão descartadas.

---

## 1. Objetivo

1. Listar os grupos de duplicata **do refatorado** com todos os sinais de decisão.
2. Eleger uma cópia canônica por grupo, de forma determinística e revisável.
3. Aplicar, com `--apply`, em transação por grupo:
   - repoint das FKs de `movement_items`, `equipment_history`,
     `service_order_items`, `maintenances` (e demais referências, se aparecerem);
   - consolidação dos campos do canônico;
   - **soft delete** da cópia excedente (`deleted_at = NOW()`);
   - decremento de `product_items.quantity` e `transaction_items.quantity`;
   - linha de auditoria por grupo.
4. Ser re-executável após cada migração e reversível por script.

## 2. Não-objetivos

- Não escreve no legado (`aluguel_legado`). O plano do legado é outro documento.
- Não muda `equipments.id` (é o id do legado), nem `number` (tombo).
- Não faz `DELETE` físico em `equipments` nem em `movement_items`.
- Não altera `docs/parquets/*.parquet`.
- **Não corrige o descarte cruzado da migração** (855 cópias de base eliminadas
  por causa de outra organização, ver §10 do plano do legado). Isso exige mexer
  em `selecionar_descartes_cliente_estoque` e reexecutar tudo — fica como D5.

---

## 3. Realidade medida (usar como baseline)

Query base (idêntica em espírito à do legado, adaptada às colunas do refatorado):

```sql
SELECT * FROM (
  SELECT eq.*, COUNT(*) OVER (PARTITION BY eq.number) AS repeticoes
  FROM equipments eq
  WHERE eq.deleted_at IS NULL
    AND eq.status_id <> 9
    AND eq.number IS NOT NULL
    AND UPPER(eq.name) NOT LIKE '%TONER%'
    AND UPPER(eq.name) NOT LIKE '%CARTUCHO%'
) t WHERE t.repeticoes > 1;
```

| Recorte | Grupos | Linhas | Excedentes |
|---|---:|---:|---:|
| Tombo global | 793 | 1.597 | 804 |
| Tombo **+ organização** | **164** | **328** | **164** |

- **637 dos 793 tombos (80%) estão em mais de uma organização** — mesmo padrão
  do legado, muitas vezes equipamento físico distinto. Deduplicar globalmente
  apaga cadastro legítimo.
- Escopo `orgao` é o único que faz sentido; `global` só como relatório.

Sinais dentro dos 164 grupos de mesma organização:

| Sinal | Grupos |
|---|---:|
| `name` divergente entre as cópias | 27 |
| `product_item_id` divergente | **26** |
| `status_id` divergente | 111 |
| têm ao menos uma cópia `status_id = 2` (ALUGADO) | 100 |
| misturam ALUGADO e DISPONÍVEL | 74 |
| têm `id` no Parquet | 25 |
| têm **2+** ids no Parquet | **5** |
| têm as duas cópias no **mesmo movimento + mesma operação** | **13** |
| têm manutenção aberta (`maintenances.is_closed = 0`) | **34** |
| têm item em OS aberta (`service_orders.status_id IN (1,2)`) | 0 |
| `product_item_id` divergente **e** `name` divergente | 26 (todos os 26 de pi) |

Somando as travas (com interseção, não soma cega): **70 grupos travados → 94
grupos descartáveis**. Esse é o lote real da v1.

Referências medidas nas 328 linhas do escopo `orgao`:

| Tabela | Coluna | Refs | Equipamentos distintos |
|---|---|---:|---:|
| `equipment_history` | `equipment_id` | 865 | 328 |
| `movement_items` | `equipment_id` | 530 | 290 |
| `service_order_items` | `equipment_id` | 387 | 238 |
| `maintenances` | `equipment_id` | 194 | 135 |
| `return_confirmations_items` | `equipment_id` | 0 | 0 |
| `volume_items` | `equipment_id` | 0 | 0 |
| `equipment_upgrades` | `equipment_id` | 0 | 0 |
| `related_equipments` | `new_/old_equipment_id` | 0 | 0 |
| `transaction_items` | `equipment_id` | 0 (migração deixa `NULL`) | 0 |

**Toda cópia tem exatamente 1 linha de entrada** (`reason =
TRANSACTION_ENTRANCE_EQUIPMENT`), criada por `migracao_equipamentos.py:985-997`;
o restante do histórico (média de 2,6 linhas por equipamento) vem dos
movimentos. São 865 linhas de histórico para 328 equipamentos no escopo
`orgao`. Isso é o que permite o tratamento do §9.3.

Estoque: `product_items.quantity` foi montado por grupo de equipamentos
idênticos (`migracao_equipamentos.py:900-945`): cada grupo insere `quantity +=
qtd_repeticoes` e todos os equipamentos do grupo apontam para o mesmo
`product_item_id`. Hoje `quantity == COUNT(equipments do pi, incluindo
soft-deleted)` em 3.303 dos 4.826 `product_items` ativos; nos outros há ajuste
manual/legado. `transaction_items.quantity` segue a mesma lógica, com
`equipments.transaction_item_id` como ligação (`transaction_items.equipment_id`
é `NULL`).

Impacto do lote aplicável (94 grupos descartáveis, §3): 94 descartes, **51
`product_items`** e até 94 `transaction_items` com `quantity` a decrementar.
Para o escopo inteiro (164 grupos) seriam 75 `pi` e 160 `ti`.

---

## 4. Decisões que bloqueiam a codificação

| # | Decisão | Opções | Recomendação |
|---|---|---|---|
| D1 | Escopo | `orgao` \| `global` | **`orgao`**. `global` exige `--permitir-global` e nunca aplica. |
| D2 | Regra do canônico | `atual` \| `historico` \| manual | **`atual`** (§6). |
| D3 | Estoque | ajustar `quantity` \| não ajustar | **Ajustar** (§9.5): mantém a tela de estoque coerente. |
| D4 | Histórico da cópia descartada | repointar tudo \| repointar e ocultar a entrada | **Repointar movimentos/OS/manutenção e soft-deletar a linha de entrada** (§9.3). |
| D5 | Restaurar as 855 cópias de base descartadas pela regra cross-org | sim \| não | **Fora deste plano.** Exige mudar a migração e reexecutar; decidir separadamente. |
| D6 | Grupos com as duas cópias no mesmo movimento+operação (13) | `NAO_MEXER` \| remover o item duplicado | **`NAO_MEXER` na v1**; tratar caso a caso na v2. |

Sem D1–D4 fechadas, o agente implementa só a Fase 0.

---

## 5. Arquitetura

```
ajustes/duplicatas_equipamentos/
├── comum.py            # engines, regra, fingerprint, leitura/escrita dos CSVs, auditoria
├── 01_extrair.py       # candidatos + sinais (read-only)
├── 02_planejar.py      # aplica a regra e gera o mapa canônico
├── 03_aplicar.py       # escreve no controle-interno (--apply; dry-run default)
├── mapa_canonico.csv   # artefato versionado, consumido pelo 03
└── README.md           # uso, revisão, rollback
```

Artefatos em `ajustes/duplicatas_equipamentos/out/`:

| Arquivo | Conteúdo |
|---|---|
| `candidatos.csv` | 1 linha por cópia, todos os sinais |
| `mapa_canonico.csv` | 1 linha por cópia com `acao` + `canonical_id` |
| `impacto.csv` | 1 linha por tabela/coluna afetada + 1 linha por `product_item`/`transaction_item` a decrementar |
| `conflitos.csv` | grupos travados com o motivo |
| `revisao.md` | resumo legível e amostra para revisão humana |
| `run_<timestamp>/` | cópia de tudo + `auditoria.csv` do que foi aplicado |

CLI:

- `--dry-run` é o padrão; escrever exige `--apply`.
- `--escopo {orgao,global}` (default `orgao`).
- `--regra {atual,historico}` (default `atual`).
- `--mapa caminho.csv` (default `mapa_canonico.csv` do diretório).
- `--limite N` aborta se o plano passar de N grupos descartáveis (default 300).
- `--somente-ids 1,2,3` aplica um subconjunto (para rollout em lotes).
- `--rollback run_<timestamp>/auditoria.csv` desfaz um lote (§10, Fase 4).

**Integração com o pipeline:** `main.py` ganha
`iniciar_ajuste_duplicatas_equipamentos`, registrado em `TAREFAS` e acrescentado
ao fim de `"todos"`. Sem `--apply` ele só grava o relatório — o pipeline
continua seguro por default.

---

## 6. Regra do canônico (determinística, sem empate)

Avaliada em ordem; o primeiro critério que separar as cópias decide.

**`--regra atual`** (default; preserva o estado operacional):

1. `id` em `IDS_LEGADO_PRIORIZADOS` (`config/parquet_excecoes.py:18`, hoje `{44764}`);
2. `id` presente em `docs/parquets/*.parquet` como `EQUIPAMENTO_ID` (o id do
   refatorado é o id do legado — ver §8);
3. `status_id` na ordem `2 ALUGADO > 1 DISPONÍVEL > 4 EMPRESTADO >
   6 EM MANUTENÇÃO > 3 RESERVADO > 5 USO INTERNO > 7 NA GARANTIA >
   8 EM DEVOLUÇÃO` (nunca `9`, que já está fora da query);
4. movimento mais recente (`MAX(movements.movement_date)` via `movement_items`);
5. maior número de `movement_items`;
6. menor `id`.

**`--regra historico`**: priorizado → Parquet → maior número de
`movement_items` → movimento mais recente → menor `id`.

Toda linha do mapa grava `regra_decisora` (qual critério decidiu), para
auditoria. Duas execuções seguidas do `02_planejar.py` têm de produzir o mesmo
arquivo byte a byte (comparar hash) — se não, há sinal faltando.

### Travas automáticas — grupo vira `NAO_MEXER`

- **2+ ids do grupo no Parquet** (5 grupos): são duas máquinas realmente
  alugadas.
- **`product_item_id` divergente dentro do grupo** (26 grupos): é o sinal mais
  forte de produto diferente apesar do mesmo número — não é duplicata.
- **As duas cópias no mesmo `movement_id` + `operation_type`** (13 grupos):
  repointar criaria dois itens do mesmo equipamento no mesmo movimento.
- equipamento com `reserved_id` preenchido (hoje 0, mas pode aparecer após a
  migração de reservas).
- equipamento com `service_order_items` em OS aberta (`service_orders.status_id
  IN (1,2)`; hoje 0 grupos) ou com manutenção aberta
  (`maintenances.is_closed = 0 AND deleted_at IS NULL`; **34 grupos**). Manutenção
  **fechada** não trava: é histórico e deve ser repointada como as demais.
- o canônico calculado está `deleted_at IS NOT NULL` ou `status_id = 9`.
- grupo com mais de 2 cópias (hoje 0 no escopo `orgao`; 28 trios existem no
  global).

---

## 7. Modelo do mapa

`ajustes/duplicatas_equipamentos/mapa_canonico.csv`, `;`, UTF-8 com BOM
(convenção de `ajustes/`):

```csv
grupo_id;escopo;tombo;orgao_id;equipment_id;canonical_id;acao;regra_decisora;status_id;tem_parquet;qtd_movimentos;ultimo_movimento;product_item_id;transaction_item_id;name;revisado_por;aprovado
1;orgao;980;1122;36811;36811;MANTER;parquet;1;S;1;2025-11-02;4102;5531;NOTEBOOK I7;matheus;S
1;orgao;980;1122;99001;36811;DESCARTAR;status;7;N;0;NULL;4102;5532;NOTEBOOK I7;matheus;S
```

Valores de `acao`:

- `MANTER` — canônico;
- `DESCARTAR` — excedente sem nada exclusivo a preservar (só soft delete);
- `MESCLAR` — excedente cujo histórico/refs serão repointados antes do soft delete;
- `NAO_MEXER` — grupo travado (§6).

`03_aplicar.py` só age em linhas `aprovado = S`; `MANTER`/`NAO_MEXER` são
ignoradas na escrita.

---

## 8. Interação com os Parquets (crítica)

`movimentos/migracao_aluguel.py:182-205` (`_carregar_equipamentos_por_id`) filtra
`deleted_at IS NULL`. Se o `id` referenciado por um Parquet for soft-deletado, a
migração cai no fallback por tombo — e pode escolher outra cópia, mudando a
operação do contrato.

Por isso:

- **todo id citado em Parquet tem de ser canônico** (trava §6);
- hoje 30 ids do escopo `orgao` aparecem no Parquet, em 25 grupos; 5 grupos têm
  dois ids no Parquet → `NAO_MEXER`;
- o `03_aplicar.py` **aborta** se o mapa tentar descartar um id do Parquet.

---

## 9. Operações de escrita (uma transação por grupo)

Ordem obrigatória. Tudo dentro de `with engine.begin()`, por grupo.

### 9.1 Guardas de entrada

- reler o grupo no banco dentro da transação e conferir que as cópias continuam
  ativas e com o mesmo `number`/`current_organization_id` do mapa;
- conferir `canonical_id` ativo;
- se qualquer um divergir → `SKIP` + linha em `conflitos.csv` (não aborta o lote).

### 9.2 Repoint das dependentes

```sql
UPDATE movement_items         SET equipment_id = :canon WHERE equipment_id = :exclu;
UPDATE service_order_items    SET equipment_id = :canon WHERE equipment_id = :exclu;
UPDATE maintenances           SET equipment_id = :canon WHERE equipment_id = :exclu;
UPDATE equipment_history      SET equipment_id = :canon WHERE equipment_id = :exclu AND reason <> 'TRANSACTION_ENTRANCE_EQUIPMENT';
UPDATE return_confirmations_items SET equipment_id = :canon WHERE equipment_id = :exclu;
UPDATE volume_items           SET equipment_id = :canon WHERE equipment_id = :exclu;
UPDATE equipment_upgrades     SET equipment_id = :canon WHERE equipment_id = :exclu;
UPDATE related_equipments     SET new_equipment_id = :canon WHERE new_equipment_id = :exclu AND new_equipment_id <> old_equipment_id;
UPDATE related_equipments     SET old_equipment_id = :canon WHERE old_equipment_id = :exclu AND old_equipment_id <> new_equipment_id;
```

- `related_equipments` tem **unique `(new_equipment_id, old_equipment_id)`** e
  **não tem `id` nem `deleted_at`**: se o par já existir com o canônico, o
  `UPDATE` falha — nesse caso `DELETE` da linha redundante (é uma relação, não
  um equipamento). Hoje há 0 refs, então é só robustez.
- `movement_items` não tem unique, mas a colisão do §6 já tirou os grupos
  problemáticos do lote.
- Nas tabelas com `deleted_at`, repointar **inclusive as linhas já
  soft-deletadas** é mais seguro (mantém a rastreabilidade); a v1 pode limitar
  a `deleted_at IS NULL` e registrar a diferença no `impacto.csv`.

### 9.3 Histórico de entrada

Cada cópia tem 1 linha `TRANSACTION_ENTRANCE_EQUIPMENT`. Repointar as duas
deixaria o canônico com duas “entradas”. Decisão D4 (recomendado):

```sql
UPDATE equipment_history
   SET deleted_at = NOW()
 WHERE equipment_id = :exclu
   AND reason = 'TRANSACTION_ENTRANCE_EQUIPMENT'
   AND deleted_at IS NULL;
```

As demais linhas de histórico (movimento, OS, manutenção) são repointadas em
§9.2.

### 9.4 Consolidação do canônico

Preencher o que estiver nulo e atualizar o “último estado”:

- `last_movement_item_customer_id` = **`movement_items.id`** do item mais
  recente entre todas as cópias. Atenção: apesar do nome, a coluna guarda o id
  do item de movimento — é o que `movimentos/migracao_movimentos.py:1279` grava
  (`last_movement_item_customer_id = :mov_item_id`). Não gravar `customer_id` ali;
- **não mexer em `movement_date`**: a coluna existe em `equipments`, mas a
  migração nunca a preenche (fica `NULL`);
- `details` = concatenar marcação legível, ex.:
  `[DUPLICATA MESCLADA: 99001 -> 36811 em 2026-11-26]` (mantém os detalhes
  originais, só acrescenta);
- `updated_at = NOW()`.

Não mexer em `name`, `number`, `serial_number`, `address_id`, `location_id`,
`product_item_id`, `transaction_item_id`, `status_id`.

### 9.5 Estoque (D3)

Só para cópias efetivamente descartadas nesta execução (ver idempotência em §9.6):

```sql
UPDATE product_items
   SET quantity = GREATEST(quantity - :k, 0), updated_at = NOW()
 WHERE id = :pi_id;
-- onde :k = quantidade de excedentes do grupo com esse product_item_id
```

Idem para `transaction_items`, usando `equipments.transaction_item_id` como
ligação (`transaction_items.equipment_id` é `NULL` na migração).

Antes/depois gravados em `impacto.csv` e `auditoria.csv`. Se `quantity < k`,
**não aplica** o decremento naquele `pi`/`ti` e registra `conflito = S` (o
estoque já estava incoerente antes do ajuste). O `GREATEST` do SQL é só a
segunda barreira.

### 9.6 Soft delete e idempotência

```sql
UPDATE equipments
   SET deleted_at = NOW(), updated_at = NOW()
 WHERE id = :exclu AND deleted_at IS NULL;
```

`rowcount = 1` → é esta execução que descartou; só então aplicar §9.5 e a
auditoria de escrita. `rowcount = 0` → já estava descartado; **pular decremento**
e registrar `SKIP_JA_APLICADO`. Isso torna o módulo seguro para rodar N vezes,
mesmo sem migração nova, sem precisar de tabela de controle.

`status_id` **não** é alterado. `9` significa baixado de verdade; a cópia
mesclada não foi baixada.

### 9.7 Auditoria

Tabela nova (fora de `TABELAS`/`TABELAS_MOVIMENTOS`, portanto sobrevive ao
truncate):

```sql
CREATE TABLE IF NOT EXISTS duplicatas_equipamentos_ajuste (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  batch_id      VARCHAR(64)  NOT NULL,
  grupo_id      INT          NOT NULL,
  tombo         INT          NULL,
  orgao_id      BIGINT UNSIGNED NOT NULL,
  canonical_id  BIGINT UNSIGNED NOT NULL,
  discarded_id  BIGINT UNSIGNED NOT NULL,
  acao          VARCHAR(20)  NOT NULL,
  regra         VARCHAR(40)  NULL,
  refs          JSON         NULL,
  quantity_pi_antes   DECIMAL(18,2) NULL,
  quantity_pi_depois  DECIMAL(18,2) NULL,
  quantity_ti_antes   DECIMAL(18,2) NULL,
  quantity_ti_depois  DECIMAL(18,2) NULL,
  aplicado_em   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revertido_em  TIMESTAMP    NULL,
  UNIQUE KEY uk_batch_discarded (batch_id, discarded_id)
);
```

Mais o `auditoria.csv` em `run_<timestamp>/` com a mesma informação.

---

## 10. Fases

### Fase 0 — Diagnóstico (read-only, ~1 dia)

- `01_extrair.py --escopo orgao` → `candidatos.csv` com, por cópia:
  `id, number, name, status_id, current_organization_id, product_item_id,
  transaction_item_id, deleted_at, created_at`;
  `qtd_movimentos, ultimo_movimento, cliente_ultimo, operacao_ultima`;
  contagem de refs por tabela (§3);
  `tem_parquet, arquivo_parquet`;
  `priorizado`, `pi_divergente_no_grupo`, `colisao_mesmo_movimento`,
  `em_os_aberta`, `em_manutencao_aberta`, `reserved_id`.
- `revisao.md` com resumo por organização e grupos travados.

Aceite:
- 328 linhas no escopo `orgao`; 1.597 no escopo `global`;
- `candidatos.csv` reproduz os 26 grupos de `product_item_id` divergente, os 5
  grupos com 2+ ids no Parquet e os 13 grupos com colisão de movimento;
- zero escrita.

### Fase 1 — Planejamento (~1 dia)

- `02_planejar.py --regra atual` → `mapa_canonico.csv`, `impacto.csv`,
  `conflitos.csv`.
- Aceite:
  - 100% das linhas classificadas; exatamente 1 `MANTER` por grupo não travado;
  - `NAO_MEXER` = **70 grupos** (união de: 26 pi divergente, 5 Parquet duplo,
    13 colisão de movimento, 34 manutenção aberta, 0 OS aberta) → **94 grupos
    descartáveis**. Se o número divergir, o `revisao.md` tem de explicar;
  - toda `DESCARTAR`/`MESCLAR` tem `canonical_id` ativo e `motivo`;
  - duas execuções → mesmo hash.

### Fase 2 — Revisão humana

- 100% dos `NAO_MEXER`; 100% dos grupos com `name` divergente (27); amostra de
  20 grupos com `status_id` divergente.
- Preencher `revisado_por` e `aprovado = S`.

Aceite: nenhum grupo com `name` divergente aprovado para descarte sem
justificativa escrita.

Como o front aplica isso: `bloqueado` = grupo com `motivo` preenchido (trava
real); aprovar responde 409. `exige_justificativa` = grupo com
`exige_revisao = S` e sem `motivo`, ou seja, só `name` divergente; aprovar
sem `justificativa` responde 422 `justificativa_obrigatoria`. Em lote, esses
grupos entram em `ignorados` e o restante é revisado normalmente.

Durante a revisão o front permite conferir as cópias no legado
(`/api/legado/equipamentos`, **read-only**): situação, órgão, movimentos e
irmãos com o mesmo tombo. Duas regras de leitura valem para essa conferência:

- **Situação atual**: `aluguel_equipamentos.situacao_id` do legado pode estar
  defasada em relação aos próprios movimentos. O estado "alugado / cliente
  atual" é derivado do movimento mais recente relevante (`tipo_id` em
  `(1, 5)` = aluguel; `(2, 3, 4, 6, 8)` = devolução, baixa, transferência,
  reserva, uso interno). A situação divergente é exibida como aviso, não como
  estado.
- **Situação atual no novo**: a fonte é o banco migrado
  (`movement_items` + `movements.recipient_customer_id` → `customers.name`),
  não o legado. O legado serve como evidência histórica para a decisão humana.

### Fase 3 — Aplicação (lotes)

1. `03_aplicar.py --dry-run` → relatório do lote.
2. `03_aplicar.py --apply --somente-ids <10 grupos>` → valida em produção de teste.
3. Conferir §11 e só então rodar o lote completo.

Aceite:
- query de duplicatas do refatorado retorna só os `NAO_MEXER`;
- `COUNT(equipments WHERE deleted_at IS NULL)` cai exatamente pelo número de
  `DESCARTAR` aplicados (o total físico não muda — é soft delete);
- `movement_items`, `service_order_items`, `maintenances` não perdem linhas
  físicas (só trocam `equipment_id`); `equipment_history` perde exatamente 1
  linha **ativa** por descarte (a entrada, §9.3);
- `product_items.quantity` cai exatamente `k` por `pi` afetado.

### Fase 4 — Pipeline e rollback

- registrar o módulo em `main.py` (`TAREFAS` + `todos`), **sem `--apply`** por
  default;
- documentar `--rollback run_<ts>/auditoria.csv`:

```sql
-- por linha revertida
UPDATE equipments SET deleted_at = NULL, updated_at = NOW() WHERE id = :discarded;
UPDATE equipment_history SET deleted_at = NULL WHERE equipment_id = :discarded AND reason='TRANSACTION_ENTRANCE_EQUIPMENT';
-- repoint reverso apenas das refs registradas em `refs` (JSON da auditoria)
UPDATE movement_items SET equipment_id = :discarded WHERE id IN (...);
UPDATE product_items SET quantity = :quantity_pi_antes WHERE id = :pi;
```

Aceite: rollback de um lote de teste devolve o banco ao estado anterior,
conferido pelas mesmas queries de §11.

---

## 11. Validação

```sql
-- 1. duplicatas por organização (deve ir a 0, exceto NAO_MEXER)
SELECT COUNT(*) FROM (
  SELECT number, current_organization_id,
         COUNT(*) OVER (PARTITION BY number, current_organization_id) c
  FROM equipments
  WHERE deleted_at IS NULL AND status_id <> 9 AND number IS NOT NULL
    AND UPPER(name) NOT LIKE '%TONER%' AND UPPER(name) NOT LIKE '%CARTUCHO%'
) x WHERE c > 1;

-- 2. nenhum equipamento ativo com refs “orfãs”
SELECT COUNT(*) FROM equipments e
WHERE e.deleted_at IS NOT NULL
  AND EXISTS (SELECT 1 FROM movement_items mi
              WHERE mi.equipment_id = e.id AND mi.deleted_at IS NULL);

-- 3. estoque coerente nos pi/ti tocados (comparar com impacto.csv)
SELECT pi.id, pi.quantity,
       (SELECT COUNT(*) FROM equipments e WHERE e.product_item_id = pi.id) n
FROM product_items pi
WHERE pi.id IN (/* ids do impacto.csv */);

-- 4. reconciliação geral
SELECT (SELECT COUNT(*) FROM equipments WHERE deleted_at IS NULL) AS ativos,
       (SELECT COUNT(*) FROM movement_items WHERE deleted_at IS NULL) AS itens,
       (SELECT COUNT(*) FROM equipment_history WHERE deleted_at IS NULL) AS hist;
```

Critérios:

- (1) = **70 grupos / 140 linhas**, exatamente os de `conflitos.csv`;
- (2) = 0;
- (3) `quantity - n` idêntico ao valor de antes do lote (o decremento acompanha
  a queda de `n`);
- (4) `itens` (movement_items ativos) = baseline; `hist` ativo = baseline −
  número de descartes (1 entrada por cópia, §9.3); `ativos` = baseline −
  descartes.

---

## 12. Riscos

| Risco | Mitigação |
|---|---|
| Deduplicar por tombo global apaga ~1.270 cópias legítimas de outras organizações | escopo `orgao` default; `global` não aplica |
| Mesmo número, produtos diferentes | trava por `product_item_id` divergente (26 grupos) |
| Soft delete de id usado no Parquet muda o contrato | trava: id do Parquet é sempre canônico; aborta se não for |
| Estoque decrementado duas vezes | decremento condicionado a `rowcount = 1` no soft delete |
| Duas cópias no mesmo movimento | trava de colisão (13 grupos) |
| Fix apagado pela próxima migração | módulo no fim do pipeline + mapa versionado |
| `equipments.id` = id do legado | nunca `DELETE`; só `deleted_at` |
| Reversão impossível | `duplicatas_equipamentos_ajuste` + `auditoria.csv` com `refs` JSON |
| `quantity` já incoerente antes | não decrementa abaixo de 0; grava conflito |

---

## 13. Briefing pronto para o agente

> Trabalhe em `/home/matheus/migracao_teste`. Leia
> `docs/plano_ajuste_duplicatas_refatorado.md` inteiro antes de escrever código.
>
> Contexto: `controle-interno` (refatorado) e `aluguel_legado` no mesmo MySQL
> local `127.0.0.1:3307`, acessíveis por `config.database.obter_engines()`
> (retorna `(novo, legado)`). O refatorado é reconstruído pelo pipeline
> (`main.py`), então qualquer escrita precisa ser um módulo pós-migração
> idempotente.
>
> Escopo desta entrega: **Fases 0 e 1**, escopo `orgao`, regra `atual`. Crie
> `ajustes/duplicatas_equipamentos/` conforme §5, com `--dry-run` default, CSVs
> em `out/`, travas de §6 e a tabela de auditoria de §9.7 (só o `CREATE TABLE`,
> sem escrever nela). Use `ajustes/limpar_duplicidades_requisicoes.py` como
> referência de estilo (argparse, dry-run, CSV, bloqueio por dependentes).
>
> Não altere `cadastros/`, `movimentos/`, `config/` nem `main.py` nesta entrega.
> Não execute `--apply`. Entregue: scripts, README, CSVs gerados e um resumo com
> as contagens de §3/§10 para revisão humana.

---

## 14. Perguntas abertas

1. O ajuste deve rodar automaticamente no fim de `main.py todos` (dry-run) ou
   só por invocação manual?
2. D3: decrementar `product_items.quantity`/`transaction_items.quantity` ou
   deixar o estoque como está?
3. D6: os 13 grupos com as duas cópias no mesmo movimento ficam `NAO_MEXER` na
   v1, ou já entram com remoção do item duplicado?
4. D5: restaurar as 855 cópias de base descartadas pela regra cross-org é
   desejado? Isso muda a contagem total do refatorado e exige reexecução da
   migração.
5. A cópia canônica deve ser sempre a **alugada**, mesmo quando a outra tem
   mais histórico? (`--regra atual` assume que sim; 74 grupos misturam
   ALUGADO e DISPONÍVEL.)
