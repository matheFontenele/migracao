# Pendências — manutenções do delta sem equipamento (execução 23/11/2026)

Execução: `run_migracao_completa.sh` de 23/11/2026 15:58 (delta 04–12/11/2026).
Decisão do usuário: **manter as capas como estão** (não religar, não excluir). Registro apenas.

## Capas com `equipment_id NULL` no destino

| capa local | origem (prod) | equipamento prod | data | fechada | detalhes |
|---|---|---|---|---|---|
| 23521 | maintenances.id 1399 | 59108 (tombo 50589) | 2025-07-21 | sim | Equipamento voltando de aluguel |
| 23522 | maintenances.id 3527 | 149718 (tombo 86348) | 2026-02-19 | não | Equipamento voltando de aluguel |
| 23505 | maintenances.id 6222 | 131979 (tombo 55415) | 2026-11-11 | não | Revisão de item devolvido pelo cliente |

Itens de manutenção vinculados às capas órfãs:

- 20282 → capa 23521 (tipo 5, "REPARO NA DOBRADIÇA, TROCA DA BATERIA E INSTALAÇÃO DO SISTEMA OPERACIONAL WINDOWS 11.")
- 20288 → capa 23522 (tipo 5, "SEM REPARO")
- capa 23505 não tem itens

## Motivo

`maintenances.equipment_id` é `NULL`-able no destino. O importador (`importar_delta_movimentos.py`,
`_ensure_maintenances` → `apply_fks`) anula a coluna em silêncio quando
`_equipment_local()` não resolve o equipamento, em vez de bloquear a linha:

- 23521: tombo 50589 duplicado no destino (equipamentos 45397 e 59108) → ambíguo.
- 23522: tombo 86348 não existe no destino (legado 149718, `situacao_id = 10`, excluído em 16/11/2026).
- 23505: tombo 55415 duplicado no destino (equipamentos 118678 e 131979) → ambíguo.

## Observações

- A capa 23521 é duplicata da capa 6932 (mesmo equipamento 59108, mesma data, mesmo detalhe),
  que permanece **aberta** localmente; a produção já a fechou.
- A produção excluiu o equipamento 118678 em 11/11/2026 e manteve 131979 (tombo 55415) em manutenção;
  as duas linhas coexistem no destino.
- As 13 capas de outubro/2026 presentes no JSON do delta não geraram órfãs: foram casadas por
  equipamento+dia com capas já migradas do legado.
- Totais de manutenção da execução (23.522 capas / 20.294 itens / 0 itens órfãos) não são afetados.
