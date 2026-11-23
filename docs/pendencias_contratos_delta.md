# Pendências de contratos no delta 04–12/11/2026

Estado após a esteira completa de 23/11/2026.

## 1. Contrato Barroquinha `20220330` não existe no banco local

- Produção: `prod_14_11.contracts.id = 271` é **PREFEITURA MUNICIPAL DE
  BARROQUINHA – SECRETARIA DE CULTURA**, número `20220330`.
- Local: `contracts.id = 271` é **Tauá – GABP**, número `20.08.001/2026-GABP`.
- Barroquinha não consta em `docs/Contratos.xlsx`, então a migração de
  contratos nunca o criou (id 271 local é outro contrato).

O delta copiava `contract_id` e `contract_item_id` pelo id cru, então a
OS 40013 (produção 17968) e seus itens 93014–93016 foram inseridos ligados
ao contrato errado.

**Tratamento aplicado:** `service_orders.contract_id` da 40013 e
`contract_item_id` dos três itens foram anulados. A OS permanece como
registro histórico, sem vínculo contratual. Se o contrato for migrado no
futuro, religar pelo número `20220330`.

## 2. Referências cruzadas herdadas do legado (67)

- Arquivo: `ajustes/refs_contrato_cruzadas_legado.csv`.
- São OS de 2018–2026 cujo `contract_item_id` aponta para item de outro
  contrato, sem `alias` na OS.
- Não há resolução determinística: por (contrato, alias, preço) o resultado é
  0 match único, 23 ambíguos e 53 sem match.
- A própria produção tem 52 casos equivalentes em 9.521 referências, ou seja,
  o defeito vem da origem.
- **Decisão de 23/11:** não alterar; o CSV serve de fila para tratamento
  manual.

`equipment_history.contract_item_id` espelha as mesmas 67 referências (o delta
não grava `contract_item_id` em histórico).

Saldo atual: `service_order_items` 13.082 referências (67 cruzadas);
`equipment_history` 13.067 referências (67 cruzadas).

## 3. Correção do importador do delta

`importar_delta_movimentos.py` v3.2.0 passa a traduzir contratos e itens de
contrato por chave natural:

- contrato → `contracts.number` normalizado;
- item → (contrato, alias, aditivo, tipo, datas), com desempate por preço e
  quantidade.

Sem correspondência única, o vínculo fica `NULL` em vez de apontar para outro
contrato. O reparo dos dados já importados é feito por
`ajustes/corrigir_contract_item_delta.py` (dry-run por padrão, idempotente).
