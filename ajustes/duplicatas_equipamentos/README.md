# Ajuste das duplicatas de equipamentos (`controle-interno`)

Módulo pós-migração que diagnostica, planeja e (opcionalmente) aplica a
consolidação das cópias duplicadas de equipamentos criadas pela migração.
Segue `docs/plano_ajuste_duplicatas_refatorado.md`, com as decisões ajustadas
listadas abaixo.

**Nada é escrito no banco nas Fases 0 e 1.** A escrita só acontece em
`03_aplicar.py --apply`, por grupo, em uma transação, e é auditada/rollbackável.

## Decisões ajustadas à realidade do schema

| Tema | Plano | Implementação |
|---|---|---|
| Escopo default | `orgao` | `orgao` (`number` + `current_organization_id`); `global` só relatório |
| Regra do canônico | `atual` | `atual` (priorizado → parquet → status → último movimento → qtd → menor id) |
| Estoque | decrementar (D3) | decrementa `product_items`/`transaction_items` só após `rowcount = 1` no soft delete |
| Histórico de entrada | repoint + ocultar (D4) | repoint das demais linhas; entrada da cópia vira `deleted_at = NOW()` |
| Colisão de movimento | `NAO_MEXER` (D6) | grupo inteiro travado |
| `transaction_items.equipment_id` | não citado | incluído no repoint (FK existe; migração deixa `NULL`) |
| Precisão da auditoria | `DECIMAL(18,2)` | `DECIMAL(18,4)` (`product_items.quantity` é `double`) |
| Idempotência | `rowcount` do soft delete | guarda de grupo ativo + `rowcount = 1` como segunda barreira |

## Arquivos

```
ajustes/duplicatas_equipamentos/
├── comum.py        # CSVs, Parquets, regra do canônico, travas
├── extrator.py     # Fase 0 (read-only)
├── planejador.py   # Fase 1 (read-only)
├── aplicador.py    # Fase 3 (dry-run/apply) + rollback
├── pipeline.py     # Fases 0+1 juntas, para o main.py
├── 01_extrair.py
├── 02_planejar.py
├── 03_aplicar.py
├── mapa_canonico.csv   # gerado pela Fase 1 (versionável/revisável)
└── out/
    ├── candidatos.csv      # Fase 0
    ├── revisao_extracao.md
    ├── impacto.csv         # Fase 1
    ├── conflitos.csv
    ├── revisao.md
    ├── mapa_canonico.sha256
    └── run_<batch>/
        ├── lote.csv
        ├── auditoria.csv   # só no --apply
        ├── skips.csv
        └── relatorio.md
```

## Uso

Sempre a partir da raiz do projeto (`migracao_teste`), com o venv:

```bash
# Fase 0 — diagnóstico (read-only)
.venv/bin/python ajustes/duplicatas_equipamentos/01_extrair.py --escopo orgao

# Fase 1 — mapa canônico (read-only)
.venv/bin/python ajustes/duplicatas_equipamentos/02_planejar.py --regra atual

# Equivalente, e é o que o main.py chamará (nunca aplica)
.venv/bin/python -m ajustes.duplicatas_equipamentos.pipeline
```

Saídas esperadas (baseline do plano §3): 164 grupos / 328 linhas no escopo
`orgao`; 70 grupos `NAO_MEXER`; 94 grupos descartáveis. Duas execuções da Fase 1
devem produzir o mesmo `sha256` do mapa.

### Revisão (Fase 2)

1. Revise 100% dos grupos travados (`out/conflitos.csv`) — não serão tocados.
2. Revise 100% dos grupos com `name` divergente (`out/revisao.md`).
3. Amostre 20 grupos com `status_id` divergente.
4. Para aprovar, preencha `revisado_por` e `aprovado = S` **em todas as linhas
   do grupo** no `mapa_canonico.csv`. Reexecutar a Fase 1 preserva a aprovação
   enquanto o grupo não mudar de composição (mesmos ids e mesmo canônico).

Atalho para os grupos sem revisão obrigatória (não travados e sem `name`
divergente): `02_planejar.py --aprovar-auto`.

### Aplicação (Fase 3)

```bash
# dry-run: mesmas guardas, sem escrever
.venv/bin/python ajustes/duplicatas_equipamentos/03_aplicar.py

# lote de teste (10 grupos)
.venv/bin/python ajustes/duplicatas_equipamentos/03_aplicar.py \
  --apply --somente-grupos 1,2,3,4,5,6,7,8,9,10

# lote completo
.venv/bin/python ajustes/duplicatas_equipamentos/03_aplicar.py --apply
```

`03_aplicar.py` só age em grupos com **todas** as linhas `aprovado = S`; grupos
travados, não aprovados ou divergentes do banco viram `SKIP` em `skips.csv` sem
interromper o lote. Erros de banco interrompem o lote (as transações já
confirmadas permanecem).

Aborta antes de qualquer escrita se o mapa tentar descartar um id citado nos
Parquets (§8) ou se o escopo for `global`.

Por grupo, na ordem do plano §9: guardas → repoint das dependentes → ocultação
da entrada do histórico → soft delete (`rowcount = 1`) → decremento de estoque →
consolidação do canônico (`last_movement_item_customer_id` + marcação em
`details`) → auditoria.

### Rollback

```bash
# confere o que seria revertido
.venv/bin/python ajustes/duplicatas_equipamentos/03_aplicar.py \
  --rollback ajustes/duplicatas_equipamentos/out/run_<batch>/auditoria.csv

# reverte de fato
.venv/bin/python ajustes/duplicatas_equipamentos/03_aplicar.py \
  --rollback ajustes/duplicatas_equipamentos/out/run_<batch>/auditoria.csv --apply
```

O rollback usa o JSON `refs` de cada linha de `auditoria.csv` (ids repontados,
pares de `related_equipments`, estoque antes/depois) e marca `revertido_em` em
`duplicatas_equipamentos_ajuste`. Linhas já revertidas são puladas.

## Auditoria

A tabela `duplicatas_equipamentos_ajuste` não está em `TABELAS` /
`TABELAS_MOVIMENTOS`, então sobrevive aos truncates da migração. O CSV
`run_<batch>/auditoria.csv` é a fonte do rollback e traz também `pi_id`/`ti_id`
e o detalhe do estoque.

## Validação (§11)

```sql
-- 1. duplicatas restantes = apenas os grupos NAO_MEXER
SELECT COUNT(*) FROM (
  SELECT number, current_organization_id,
         COUNT(*) OVER (PARTITION BY number, current_organization_id) c
  FROM equipments
  WHERE deleted_at IS NULL AND status_id <> 9 AND number IS NOT NULL
    AND UPPER(name) NOT LIKE '%TONER%' AND UPPER(name) NOT LIKE '%CARTUCHO%'
) x WHERE c > 1;

-- 2. nenhuma referência ativa apontando para cópia descartada
SELECT COUNT(*) FROM equipments e
WHERE e.deleted_at IS NOT NULL
  AND EXISTS (SELECT 1 FROM movement_items mi
              WHERE mi.equipment_id = e.id AND mi.deleted_at IS NULL);

-- 3. estoque coerente (comparar com impacto.csv)
SELECT pi.id, pi.quantity,
       (SELECT COUNT(*) FROM equipments e WHERE e.product_item_id = pi.id) n
FROM product_items pi
WHERE pi.id IN (/* ids do impacto.csv */);
```

Critérios: (1) 70 grupos / 140 linhas; (2) 0; (3) `quantity - n` igual ao de
antes do lote; (4) `movement_items` ativos inalterados, `equipment_history`
ativo cai 1 por descarte, `equipments` ativos caem pelo número de descartes.

## Integração futura (Fase 4)

`pipeline.executar(eng_novo, eng_legado, argumentos_cli)` roda Fases 0 e 1 e
retorna os stats, pronto para entrar em `TAREFAS`/`GRUPOS` do `main.py` **sem
`--apply`**. A aplicação continua sendo um comando manual e auditado.
