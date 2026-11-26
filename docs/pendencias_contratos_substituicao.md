# Contratos nas substituições — vínculo cruzado (corrigido em 24/11/2026)

## Sintoma

Requisição de Aquiraz (SO 7209, `Aluguel Passado - Origem da Substituição: 544175`)
aparecia atrelada ao contrato **105 — Prefeitura Municipal de Camocim / Esporte**,
com o item 3821 (NOTEBOOK TIPO II), embora o equipamento fosse uma multifuncional
Canon G3110 e o cliente, Aquiraz.

## Causa

O legado não guarda contrato por movimento. O módulo de substituição usava a linha
do **Parquet indexada pelo tombo**, sem conferir se o cliente da linha era o mesmo
do movimento. O tombo do equipamento novo (47918) existia no Parquet com contrato
de outro cliente (Camocim), e o tombo antigo (76301) não existia — o fallback usava
o novo. Sem item na linha, `regras_item_contratos` caía no "primeiro item do
contrato", produzindo o NOTEBOOK.

Além disso, `df_passado` pegava o **último** aluguel do equipamento sem limite de
data: a substituição de 2024 puxou um aluguel de 2025, de outro cliente (Aquiraz).

## Política aplicada

1. **Cronologia**: o aluguel histórico é o aluguel vigente **anterior** à data da
   devolução da substituição. Ciclo sem aluguel anterior válido continua existindo
   (devolução/envio), apenas sem a fase de aluguel passado.
2. **Contrato**, nesta ordem de confiança:
   - número do contrato embutido no **endereço de entrega** do cliente
     (ex.: `...-20252208.12` → contrato 240);
   - linha do **Parquet somente se o `CLIENTE_ID` for o mesmo** do movimento;
   - caso contrário, **avulso** (sem contrato).
3. **Guarda de cliente**: o contrato escolhido precisa estar vinculado ao cliente
   do movimento (dono em `contracts.customer_id` ou destinatário em
   `contract_recipient_customers`). Contrato de outro cliente é descartado.
4. **Item**: só é aceito se pertencer ao contrato; senão, resolvido por
   similaridade (Jaccard de tokens) com o nome do equipamento, exigindo melhor
   score isolado. Nunca mais "primeiro item do contrato".
5. **Fase histórica**: quando o cliente de origem difere do cliente da
   substituição, o contrato/item são resolvidos pelo endereço de origem.

## Efeito medido (base local de 24/11)

- 17.217 linhas de substituição avaliadas;
- ~1.293 linhas com contrato validado (161 por endereço, 1.132 por Parquet do
  mesmo cliente);
- 242 vínculos do Parquet descartados por serem de outro cliente;
- ~1.150 linhas com contrato e item resolvidos (antes, ~2.500 vínculos cruzados).

Os ajustes ficam registrados em `log_contratos_substituicao.csv` (ignorado pelo
git) e as divergências em `log_divergencias_substituicao.csv`.
