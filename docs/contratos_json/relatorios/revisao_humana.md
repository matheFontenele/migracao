# Revisão humana — contratos novos de Redenção

Rodada de extração das fichas de Redenção (CE) para o formato
`contratos_json` (schema 1). Nada foi enviado ao banco; os arquivos estão em
`_novos/` e dependem de aprovação para carga.

## Resumo

- 20 contratos novos em `_novos/` (ALUCOM 9, IP 2, MOREIA 9).
- 42 itens, 27 eventos (aditivos incluídos).
- Soma dos valores declarados por evento: R$ 482.007,84.
- `source_contract_id: null` em todos: nenhum número consta de
  `_referencia/contracts_snapshot.csv`.
- `_pendentes/` permanece vazio; a carga não é bloqueada por estes arquivos.
- `manifest.json`: `files` continua com os 267 contratos já carregados e
  `novos` passa a 20.

Validação executada com o próprio importador
(`importar_contratos_json._validar_documento`) sobre os 20 arquivos:
**0 erros, 0 avisos**. A geração foi repetida duas vezes e os 20 arquivos
saíram byte a byte idênticos (sha256 agregado
`4a38b2088dcbcb5c324c945b8afdf94c7c30d8f29a06d70f6f88bfdcc09f5c28`).

## Como o cliente foi resolvido

`customer_alias` é sempre o alias raiz da organização; `recipient_aliases`
traz o alias da secretaria/órgão que consta em `customers.csv`, **sem inventar
alias**. Em 19 dos 20 casos o alias da secretaria tem `parent_alias` igual à
raiz usada. A exceção está anotada em `relatorios/pendencias.csv`.

| Contrato | `recipient_aliases` | `parent_alias` em customers | `customer_alias` usado |
| --- | --- | --- | --- |
| 016/2026-001 | SECRETARIA DE ASSISTÊNCIA SOCIAL E DIREITOS HUMANOS DO MUNICIPIO DE REDENÇÃO - 016/2026-002 PE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE |
| 016/2026-002 | P.M. DE REDENÇÃO - SECRETARIA DE ADMINISTRAÇÃO - 016/2026-002 PE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE |
| 016/2026-003 | INSTITUTO MUNICIPAL DE PREVIDÊNCIA SOCIAL DA PREFEITURA DE REDENÇÃO | PREFEITURA MUNICIPAL DE REDENÇÃO - CE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE |
| 016/2026-004 | CONTROLADORIA GERAL DO MUNICIPIO DE REDENÇÃO - 016/2026-002 PE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE |
| 016/2026-005 | GABINETE DO PREFEITO DO MUNICIPIO DE REDENÇÃO - 016/2026-002 PE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE |
| 016/2026-006 | P.M. DE REDENÇÃO - SECRETARIA DE FINANÇAS - 016/2026-006 PE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE |
| 016/2026-007 | SECRETARIA DE CULTURA MUNICIPIO DE REDENÇÃO - 016/2026-002 PE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE |
| 016/2026-008 | SECRETARIA DE EDUCAÇÃO MUNICIPIO DE REDENÇÃO - 016/2026-002 PE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE | PREFEITURA MUNICIPAL DE REDENÇÃO - CE |
| 016/2026-009 | P. M. DE REDENÇÃO - SECRETARIA DE SAÚDE | — (sem pai) | PREFEITURA MUNICIPAL DE REDENÇÃO - CE |
| 007/2026-001 | INSTITUTO DE PREVIDÊNCIA SOCIAL DO MUNICIPIO DE REDENÇÃO | PREFEITURA MUNICIPAL DE REDENÇÃO | PREFEITURA MUNICIPAL DE REDENÇÃO |
| 007/2026-002 | CONTROLADORIA GERAL DO MUNICÍPIO DE REDENÇÃO | PREFEITURA MUNICIPAL DE REDENÇÃO | PREFEITURA MUNICIPAL DE REDENÇÃO |
| 006/2022-001-AD | SECRETARIA MUNICIPAL DE EDUCACAO DO MUNICIPIO DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO |
| 014/2024-001 | SECRETARIA DE ADMINISTRAÇÃO DO MUNICIPIO DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO |
| 014/2024-002 | SECRETARIA MUNICIPAL DE AGRICULTURA, MEIO AMBIENTE E DESENVOLVIMENTO URBANO | PREFEITURA MUNICIPAL DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO |
| 014/2024-003 | SECRETARIA DE INFRAESTRUTURA DO MUNICIPIO DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO |
| 014/2024-004 | SECRETARIA DE ESPORTE DO MUNICIPIO DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO |
| 014/2024-005 | GABINETE DO PREFEITO DO MUNICIPIO DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO |
| 014/2024-006 | SECRETARIA DE INFRAESTRUTURA DO MUNICIPIO DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO |
| 014/2024-007 | SECRETARIA DE FINANÇAS DO MUNICIPIO DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO |
| 014/2024-009 | SECRETARIA DE ESPORTE DO MUNICIPIO DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO | PREFEITURA MUNICIPAL DE REDENCAO |

Observações:

- Os aliases ALUCOM terminados em `- 016/2026-002 PE` pertencem a outras
  secretarias, mas existem assim em `customers.csv`; foram usados literalmente.
- `016/2026-009` é o único alias sem `parent_alias`. A raiz escolhida é o
  alias da própria organização (`PREFEITURA MUNICIPAL DE REDENÇÃO - CE`);
  como o importador resolve a raiz com `parent_id or id`, o vínculo fecha,
  mas a relação precisa de confirmação humana.

## Fichas com dois contratos (MOREIA)

As abas mensais de ESPORTE e INFRAESTRUTURA têm **duas colunas de contrato**
na mesma linha de item. Cada contrato virou um arquivo, com a quantidade
rateada pela coluna da respectiva secretaria:

| Ficha / aba | Contratos | Itens e rateio |
| --- | --- | --- |
| `REDENÇÃO - PLANILHA ESPORTE- ATA 142024001 (DR).xlsx` / ESPORTE OUT26 | 014/2024-004 e 014/2024-009 | microcomputador: 3 + 2 (coluna C + D; total 5) |
| `REDENÇÃO - PLANILHA INFRAESTRUTURA - ATA 142024001 (DR).xlsx` / INFRAESTRUTURA OUT26 | 014/2024-003 e 014/2024-006 | 003: 2 notebooks; 006: 1 micro + 1 notebook |

O `R$ GLOBAL/ANUAL` dessas linhas cobre a soma das duas colunas; o valor de
cada JSON é `quantidade_da_coluna × preço × meses`, e a soma dos dois
contratos fecha o total da ficha (ESPORTE 8.107,20; INFRAESTRUTURA 7.741,80).

## Aditivos

- `006/2022-001-AD` (EDUCAÇÃO): 5 eventos — cadastro (28/07/2022 a
  28/07/2023) e quatro aditivos de prazo, um por ciclo de 12 meses, até
  24/07/2027. Cada evento replica o item com a mesma `lineage_key` e
  `months` ajustado ao ciclo.
- `014/2024-002` (AGRICULTURA): 2 eventos (cadastro + 1º aditivo de prazo).
- Nos demais, um único evento de cadastro.

## Aliases de item

O alias é derivado da descrição: normaliza espaços, remove data e bullet no
início, aproveita a primeira linha quando ela já é um rótulo, corta em
`-`/`–`/`—` e, se ainda passar de 120 caracteres, escolhe o menor corte
válido em `:`, `;`, `,` ou ponto final (sem quebrar decimais como `3.20GHZ`).
Limite duro de 255 caracteres.

Em quatro contratos ALUCOM (`016/2026-001`, `-004`, `-006`, `-008`) dois
produtos distintos compartilham o alias `LOCAÇÃO DE IMPRESSORAS
MULTIFUNCIONAIS` porque a descrição da planilha começa igual para ambos
(laser monocromático a R$ 290,54 e tanque de tinta colorido a R$ 189,51). A
distinção é feita por `occurrence` 0/1, com preços consistentes nos quatro
contratos. Está registrado em `relatorios/avisos.csv` e na fila de revisão.

## Valores e status

- `total_amount` é `Σ(quantidade × preço × meses)`; `months` vem da coluna
  `MÊS` da ficha (12 em todos os itens) e não foi arredondado para fechar
  totais.
- `contract_status` calculado com data de referência fixa **25/11/2026**:
  ALUCOM/IP em `EM_ANDAMENTO`; MOREIA 014/2024-* em `ENCERRADO`;
  `006/2022-001-AD` em `EM_ANDAMENTO` (último evento termina em 24/07/2027).
- `info` foi materializado em todos os aditivos (sem `null`).

## Pendências e decisões

A fila está em `relatorios/pendencias.csv` (5 linhas: 4 `revisar`, 1
`informativa`). Nenhuma é bloqueante. Os pontos que pedem confirmação:

1. Vínculo do alias `P. M. DE REDENÇÃO - SECRETARIA DE SAÚDE` (sem pai).
2. Rateio das fichas de ESPORTE e INFRAESTRUTURA entre os dois contratos.
3. Ordem estável dos dois produtos que compartilham o alias de impressoras.

## Arquivos gerados

- `_novos/ALUCOM/016-2026-{001..009}.json`
- `_novos/IP/007-2026-{001,002}.json`
- `_novos/MOREIA/006-2022-001-AD.json`, `_novos/MOREIA/014-2024-{001..007,009}.json`
- `relatorios/fontes.csv`, `cobertura.csv`, `novos_contratos.csv`,
  `avisos.csv`, `pendencias.csv`
- `manifest.json` atualizado (`novos: 20`)
