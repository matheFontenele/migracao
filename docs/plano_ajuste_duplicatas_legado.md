# Plano — Ajuste das cópias duplicadas de `aluguel_equipamentos`

Documento de handoff. Escrito para ser delegado a um agente implementador.
Base: snapshot local `aluguel_legado` (MySQL 8, `127.0.0.1:3307`) e código atual do
repositório `migracao_teste`.

---

## 0. Achado que redefine o escopo (ler antes de qualquer coisa)

A lista de 3.434 linhas do legado **não é composta majoritariamente por duplicata**:

| Recorte | Grupos | Linhas | Excedentes |
|---|---:|---:|---:|
| Tombo global (query atual) | 1.703 | 3.434 | 1.731 |
| Tombo **+ organização** | **183** | **366** | **183** |

- **1.528 dos 1.703 grupos (90%) são o mesmo número em organizações diferentes.**
- Em muitos casos é equipamento **fisicamente distinto**: tombo `980` é
  `IMPRESSORA EPSON LX 300` na org `1115` e `NOTEBOOK I7 LENOVO` na org `1122`.
- Por isso, deduplicar por tombo global **apagaria ~1.500 cadastros legítimos**.

Além disso, a regra atual de descarte (`selecionar_descartes_cliente_estoque`,
`cadastros/equipamentos/migracao_equipamentos.py:77-105`) agrupa por tombo
**global**. Efeito medido:

- 926 cópias de base/estoque foram descartadas na última migração;
- **855 delas não têm nenhuma duplicata na própria organização** — foram
  eliminadas por causa de uma cópia de outra org (ex.: id `36811`, tombo `980`,
  org `1122`, descartado por causa do id `980` da org `1115`).

**Consequência para o plano:** o sistema de ajuste só faz sentido com escopo
`tombo + organização`. O escopo global deve existir apenas como diagnóstico, com
trava explícita.

---

## 1. Objetivo

Criar uma ferramenta simples, auditável e reversível que:

1. lista os grupos de cópias duplicadas do legado com todos os sinais de decisão;
2. elege **uma cópia canônica** por grupo, de forma determinística;
3. aplica o ajuste em **um** de dois modos:
   - **Modo A (recomendado)** — mapa de/para consumido pela migração, sem escrita no legado;
   - **Modo B** — mesclagem física no legado (repoint de FKs + soft delete da cópia excedente);
4. produz trilha de auditoria e permite reverter.

## 2. Não-objetivos

- Não altera o banco refatorado (`controle-interno`) diretamente.
- Não altera `docs/parquets/*.parquet` nem `EQUIPAMENTO_ID`.
- Não faz `DELETE` físico em `aluguel_equipamentos` (FKs e o id do legado é o id
  do refatorado — ver §6).
- Não mexe em `EQUIPAMENTOS_IGNORADOS`/`IDS_LEGADO_PRIORIZADOS` à mão; o mapa é
  o novo canal.

---

## 3. Decisões que bloqueiam a codificação

| # | Decisão | Opções | Recomendação |
|---|---|---|---|
| D1 | Escopo do agrupamento | `orgao` \| `global` | **`orgao`** (183 grupos). `global` só como relatório. |
| D2 | Como aplicar | mapa na migração (A) \| merge no legado (B) | **A primeiro**; B só se o legado precisar parar de mostrar duplicata na tela. |
| D3 | Regra do canônico | `atual` \| `historico` \| manual | **`atual`** (ver §5). |
| D4 | Destino das 855 descartadas indevidamente | restaurar \| manter | **Restaurar**: corrigir o agrupamento da regra para `tombo + org`. |

Sem D1–D4 fechadas, o agente implementa só a Fase 0 (diagnóstico).

---

## 4. Arquitetura

```
ajustes/duplicatas/
├── comum.py         # conexões, carga da regra, fingerprint, escrita de CSV
├── 01_extrair.py    # materializa candidatos + sinais
├── 02_planejar.py   # aplica a regra, gera mapa_canonico.csv + relatório de impacto
├── 03_aplicar.py    # modo migracao (nada a gravar) | modo legado (--apply)
└── README.md        # uso, exemplos, como revisar
```

Artefatos em `ajustes/duplicatas/out/`:

| Arquivo | Conteúdo |
|---|---|
| `candidatos.csv` | 1 linha por cópia, com todos os sinais |
| `mapa_canonico.csv` | 1 linha por cópia, com `acao` e `canonical_id` |
| `impacto.csv` | 1 linha por tabela dependente afetada |
| `revisao.md` | resumo legível: N grupos, N canônicos, conflitos para revisão manual |
| `fingerprint.json` | contagem + `MAX(updated_at)` + hash dos ids, para travar o `--apply` |

### Comportamento

- `--dry-run` é o padrão; escrever exige `--apply`.
- `--escopo {orgao,global}` (default `orgao`).
- `--regra {atual,historico}` (default `atual`).
- `--modo {migracao,legado}` (default `migracao`).
- `--limite N` aborta se o plano passar de N grupos (default 500).
- Toda execução grava `out/run_<timestamp>/` com cópia dos CSVs.

### Por que o Modo A é o recomendado

- O id do legado é o id do refatorado (`equipments.id = aluguel_equipamentos.id`):
  apagar no legado muda o universo de ids e quebra reexecuções.
- A migração já tem o encaixe: `EQUIPAMENTOS_IGNORADOS` é um mapa nominal de
  descarte (`migracao_equipamentos.py:482-495`). O mapa generaliza esse encaixe.
- Movimentos já são atribuídos por tombo (`migracao_movimentos.py:403-406`,
  `:690-695`), então a mesclagem de histórico acontece de graça.
- Reverter = remover o CSV e rodar a migração de novo.

---

## 5. Regra do canônico (determinística, sem empate)

Avaliada em ordem; o primeiro critério que separar as cópias decide.

**`--regra atual`** (default; preserva o estado operacional vigente):

1. id em `IDS_LEGADO_PRIORIZADOS` (`config/parquet_excecoes.py:18`);
2. id presente em `docs/parquets/*.parquet` como `EQUIPAMENTO_ID`;
3. `situacao_id` na ordem de prioridade `1 (alugado) > 7 (liberado) > 3 (manutenção) > 5 > 2 > 6 > 9 > 14 > 10`;
4. movimento mais recente (`MAX(aluguel_movimento.data)`);
5. menor `id`.

**`--regra historico`** (para o modo de descarte puro):

1. id priorizado; 2. id no Parquet; 3. **maior quantidade de movimentos**;
4. movimento mais recente; 5. menor `id`.

### Travas automáticas — grupo vira `NAO_MEXER`

- 2+ ids do grupo aparecem no Parquet (hoje: **5 grupos**) — são duas máquinas
  realmente alugadas.
- a cópia excedente é referenciada por `numero_antigo` de equipamento ativo;
- o grupo tem linha em `equipamentos_retombo`;
- a cópia excedente está em `IDS_LEGADO_PRIORIZADOS`;
- alguma cópia tem `situacao_id = 10` (baixado/retombado).

---

## 6. Modelo do mapa

`mapa_canonico.csv` (separador `;`, UTF-8 com BOM, mesma convenção de `ajustes/`):

```csv
grupo_id;escopo;tombo;orgao_id;id_legado;canonical_id;acao;regra_decisora;score;tem_parquet;qtd_movimentos;ultimo_movimento;situacao_id;cliente_ultimo;nome;motivo
1;orgao;980;1122;36811;36811;MANTER;parquet;0;1;3;2025-11-02;1;4029;NOTEBOOK I7;-
1;orgao;980;1122;99001;36811;DESCARTAR;parquet;-1;0;0;NULL;7;10468;NOTEBOOK I7;copia base sem historico
```

Valores de `acao`:

- `MANTER` — cópia canônica;
- `MESCLAR` — cópia excedente cujo histórico/FKs devem ser remapeados para o
  canônico (só relevante no Modo B ou no remap de leitura);
- `DESCARTAR` — cópia excedente sem nada a preservar;
- `NAO_MEXER` — grupo travado por uma das regras de §5.

Colunas de revisão humana (preenchidas no `02_planejar.py`): `revisado_por`,
`aprovado` (`S`/`N`). O `03_aplicar.py` só age em linhas com `aprovado = S`.

---

## 7. Fases

### Fase 0 — Diagnóstico (1 dia, read-only)

Entregáveis:

- `01_extrair.py --escopo orgao` gerando `candidatos.csv` com os sinais:
  - dados da cópia (`id, numero, orgao_id, nome, situacao_id, deleted_at`);
  - `qtd_movimentos`, `ultimo_movimento`, `cliente_ultimo`, `tipo_ultimo`;
  - contagem de referências por tabela (lista em §8);
  - `tem_parquet`, `arquivo_parquet`, `contrato_parquet`;
  - `priorizado`, `base_estoque`, `numero_antigo`, `referenciado_por_numero_antigo`, `retombo`;
- `revisao.md` com o resumo por organização e a lista dos grupos travados.

Aceite:

- 366 linhas no escopo `orgao`; 3.434 no escopo `global` (para conferência);
- nenhuma escrita no banco.

### Fase 1 — Planejamento (1 dia)

Entregáveis: `02_planejar.py` + `mapa_canonico.csv` + `impacto.csv`.

Aceite:

- 100% das linhas classificadas, exatamente 1 `MANTER` por grupo não travado;
- `NAO_MEXER` reproduz os 5 grupos com 2+ ids no Parquet;
- toda cópia `DESCARTAR`/`MESCLAR` tem `motivo` preenchido e `canonical_id` válido;
- rodar duas vezes seguidas gera o mesmo mapa (determinismo — comparar hash).

### Fase 2 — Revisão humana

- Conferência amostral: 20 grupos aleatórios + 100% dos `NAO_MEXER` + 100% dos
  grupos com status divergente.
- Marcar `aprovado = S` no CSV.

Aceite: `impacto.csv` sem nenhuma linha com `conflito = S` sem justificativa.

### Fase 3A — Modo migração (recomendado)

Alterações de código:

1. `cadastros/equipamentos/migracao_equipamentos.py`
   - carregar `mapa_canonico.csv` no início da execução;
   - tratar `DESCARTAR` no mesmo ponto de `EQUIPAMENTOS_IGNORADOS` (`:482`);
   - **corrigir** `selecionar_descartes_cliente_estoque` para agrupar por
     `(tombo, orgao)` — restaura as 855 cópias (D4);
   - registrar no relatório de descartes o motivo `MAPA_CANONICO`.
2. `movimentos/migracao_movimentos.py`
   - em `selecionar_equipamento_por_tombo`, consultar o mapa antes da heurística:
     se o tombo está no mapa, retornar o `canonical_id` (hoje o desempate é
     menor `id`, que pode escolher a cópia errada — ver §9);
   - idem em `migracao_aluguel.resolver_equipamento_do_parquet` como fallback.
3. Teste novo: `test_mapa_canonico.py` cobrindo mapa vazio, mapa com
   `NAO_MEXER`, e precedência sobre a heurística.

Aceite:

- reexecutar a migração completa no ambiente de teste;
- query de duplicatas retorna **apenas** os grupos `NAO_MEXER`;
- `COUNT(*) equipments` = anterior − descartes + restauradas (855);
- `COUNT(*) movement_items` e `equipment_history` reconciliados com o esperado.

### Fase 3B — Modo legado (opcional, só após 3A estabilizado)

`03_aplicar.py --modo legado --apply`, por grupo, em transação:

1. `mysqldump` automático das tabelas afetadas antes de qualquer escrita;
2. repoint dos dependentes (§8) de `id_excedente` → `canonical_id`;
3. consolidar campos do canônico: preencher nulos com dados da cópia
   (`nome`, `acessorio`, `nota_fiscal`, `data_aquisicao`, `data_garantia`);
4. `UPDATE aluguel_equipamentos SET deleted_at = NOW() WHERE id = :excedente`
   (soft delete; **não** alterar `situacao_id` para 10 — 10 significa baixado);
5. gravar linha em `auditoria_ajuste_duplicatas.csv`.

Travas: aborta se o `fingerprint.json` divergir do banco; aborta se algum
dependente tiver conflito semântico (mesmo movimento já contendo o canônico).

### Fase 4 — Verificação e entrega

- rodar as queries de §9 no legado e no refatorado;
- anexar `revisao.md`, `mapa_canonico.csv` e `auditoria_ajuste_duplicatas.csv`;
- documentar no README como reverter (Modo A: apagar mapa e reexecutar; Modo B:
  script de rollback a partir do dump + auditoria).

---

## 8. Superfície de impacto (medida no snapshot)

Tabelas que referenciam `aluguel_equipamentos.id` (FKs reais):

| Tabela | Coluna | Refs nas 3.434 |
|---|---|---:|
| `aluguel_movimento_itens` | `equipamento_id` | 11.540 |
| `aluguel_movimentos_ATUALIZADOS` | `COD_EQUIPAMENTO` (sem FK) | 3.356 |
| `equipamentos_localizados` | `equipamento_id` | 2.042 |
| `movimento_faturamento_itens` | `equipamento_id` (sem FK) | 1.297 |
| `devolucoes_confirmadas_equipamentos` | `equipamento_id` | 1.169 |
| `aluguel_manutencao_movimento` | `equipamento_id` | 850 |
| `aluguel_lista_equipamentos` | `equipamento_id` | 54 |
| `equipamentos_retombo` | `equipamento_novo_id` / `equipamento_antigo_id` | 13 |
| `aluguel_equipamentos` | `numero_antigo` | 53 |
| `aluguel_proposta_itens` | `equipamento_id` | 1 |
| `aluguel_lancamento_itens`, `aluguel_mantencao_equipamentos`, `garantia_movimentos` | `equipamento_id` | 0 |

A migração **lê** apenas: `aluguel_equipamentos`, `aluguel_movimento_itens`,
`aluguel_manutencao_*`, `aluguel_situacao`, `aluguel_tipos`, `ORGAOS`,
`aluguel_clientes`. As demais só importam no Modo B.

---

## 9. Validação

Queries de aceite (todas read-only):

```sql
-- 1. duplicatas por organização (escopo correto)
SELECT COUNT(*) FROM (
  SELECT numero, orgao_id, COUNT(*) OVER (PARTITION BY numero, orgao_id) c
  FROM aluguel_equipamentos
  WHERE deleted_at IS NULL AND situacao_id <> 10
    AND UPPER(nome) NOT LIKE '%TONER%' AND UPPER(nome) NOT LIKE '%CARTUCHO%'
) x WHERE c > 1;

-- 2. grupos travados (2+ ids no Parquet) — deve bater com o mapa
--    (executar em Python lendo docs/parquets, cruzando com a lista acima)

-- 3. pós-migração: duplicatas no refatorado
SELECT COUNT(*) FROM (
  SELECT number, current_organization_id, COUNT(*) OVER (PARTITION BY number, current_organization_id) c
  FROM equipments
  WHERE deleted_at IS NULL AND status_id <> 9
    AND UPPER(name) NOT LIKE '%TONER%' AND UPPER(name) NOT LIKE '%CARTUCHO%'
) x WHERE c > 1;

-- 4. reconciliação de volumes
SELECT (SELECT COUNT(*) FROM equipments) AS equipamentos,
       (SELECT COUNT(*) FROM movement_items) AS itens_movimento,
       (SELECT COUNT(*) FROM equipment_history) AS historico;
```

Critérios:

- (1) = 183 antes do ajuste; (3) = 0 (ou só os `NAO_MEXER`);
- (4) idêntico ao baseline registrado antes da reexecução, exceto pelos
  descartes planejados;
- reexecução do mapa duas vezes → mesmo hash.

---

## 10. Riscos

| Risco | Mitigação |
|---|---|
| Deduplicar por tombo global apaga ~1.500 equipamentos legítimos | escopo `orgao` default; `global` exige `--permitir-global` |
| Apagar cópia que está alugada no Parquet | trava §5 + validação de 151 ids no Parquet |
| Perder histórico de movimento | Modo A remapeia por tombo; Modo B repointa itens |
| Quebrar retombamento | trava por `numero_antigo` e `equipamentos_retombo` |
| Id do legado = id do refatorado | nunca DELETE físico; só soft delete |
| Reexecução com banco alterado | `fingerprint.json` + aborto |
| Regra cross-org da migração atual | Fase 3A item 1 (correção do agrupamento) |

---

## 11. Briefing pronto para o agente

> Trabalhe em `/home/matheus/migracao_teste`. Leia
> `docs/plano_ajuste_duplicatas_legado.md` inteiro antes de escrever código.
>
> Contexto: snapshot local do legado em `127.0.0.1:3307` (`aluguel_legado`) e
> refatorado no mesmo servidor (`controle-interno`), ambos acessíveis por
> `config.database.obter_engines()` (retorna `(novo, legado)`). Não há escrita
> autorizada no legado nesta fase.
>
> Escopo desta entrega: **Fases 0 e 1** do plano, com escopo `orgao` e regra
> `atual`. Crie `ajustes/duplicatas/` conforme §4, com `--dry-run` default,
> CSVs em `ajustes/duplicatas/out/` e as travas automáticas de §5. Siga o estilo
> de `ajustes/limpar_duplicidades_requisicoes.py` (argparse, dry-run, CSV de
> auditoria, bloqueio por dependentes).
>
> Não altere `cadastros/`, `movimentos/`, `config/` nem o banco refatorado nesta
> entrega. Não execute `--apply`. Entregue: scripts, README, os CSVs gerados e
> um resumo com as contagens de §7 (Fase 0/1) para revisão humana.

---

## 12. Perguntas abertas

1. O legado precisa parar de exibir a duplicata para o operador (Modo B), ou
   basta a migração sair limpa (Modo A)?
2. As 855 cópias descartadas indevidamente devem ser restauradas na próxima
   migração? Isso muda a contagem de equipamentos e precisa de aval.
3. A cópia canônica deve ser a **alugada** mesmo quando a outra tem mais
   histórico? (`--regra atual` assume que sim.)
4. `aluguel_movimentos_ATUALIZADOS` e `movimento_faturamento_itens` são
   reconstruídos por rotina do legado ou precisam de repoint manual?
