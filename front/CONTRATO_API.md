# Contrato do front de consulta ao legado

Ferramenta **local, single-user**, em português (pt-BR), para **consultar e
visualizar** o banco legado `aluguel_legado`. O front é somente leitura: não há
revisão, aprovação, dry-run, aplicação nem rollback. O pipeline de ajuste
continua existindo, mas apenas como CLI em `ajustes/duplicatas_equipamentos`.

Regras que a UI **nunca** pode quebrar:

1. Nenhuma rota do front escreve no legado; todos os acessos são `SELECT`.
2. Nenhum `POST`, `PUT`, `PATCH` ou `DELETE` no front.
3. Nenhum `DELETE` físico, em lugar nenhum.
4. A UI não corrige dados: apenas mostra o cadastro, os movimentos e sinaliza
   divergências (`situacao_divergente`).

---

## 1. Convenções

- Base URL relativa (`/api/...`), JSON em `application/json; charset=utf-8`.
- Datas já vêm formatadas pelo backend: `dd/mm/aaaa` ou `dd/mm/aaaa HH:MM`.
  A UI não reformata nem converte fuso.
- Erros: `{"erro": "codigo_curto", "mensagem": "texto para o usuário", ...}`.
  Códigos usados: `filtro_invalido` (422), `tombo_inexistente` (404),
  `equipamento_inexistente` (404), `movimento_inexistente` (404),
  `consulta_falhou` (500), `banco_indisponivel` (503).
- Não há paginação por cursor: todas as listas usam `page`/`per_page`.

## 2. Páginas

Todas renderizam `templates/consulta.html`; a navegação entre níveis é feita no
cliente com `history.pushState`.

| rota | conteúdo |
| --- | --- |
| `GET /` | redireciona para `/consulta` |
| `GET /consulta` | cards de tombos + filtros + paginação |
| `GET /consulta/tombo/<numero>` | cópias daquele tombo (deep link) |
| `GET /consulta/equipamento/<id>` | histórico da cópia (deep link) |
| `GET /consulta/<id>` | redireciona para `/consulta/equipamento/<id>` preservando a query string |

O template recebe `numero` e `legado_id` (ou `None`) e os expõe no `<body>`
como `data-numero` e `data-legado-id`, para abrir o nível correto ao carregar.

## 3. `GET /api/saude`

```json
{"ok": true, "legado": "ok", "modo": "consulta"}
```

`legado` vira `"erro: ..."` (e `ok` vira `false`) quando a conexão falha.

## 4. `GET /api/consulta/filtros`

```json
{
  "ultimo_equipamento": 106611,
  "orgaos": [{"id": 1, "nome": "..."}],
  "tipos": [{"id": 1947, "nome": "MICRO I5"}],
  "situacoes": [{"id": 1, "nome": "LIBERADO P/ LOCACAO", "qtd": 62718}],
  "chips": [{"chave": "alugados", "rotulo": "Alugados", "situacao_id": 2,
             "tom": "verde", "qtd": 6}]
}
```

- `situacoes`/`chips` usam o rótulo do sistema antigo: `1` aparece como
  `LIBERADO P/ LOCACAO`; `14` (que não existe mais em `status`) como
  `SEM CONSERTO`.
- Chips, na ordem da tela antiga (o `tom` é dica de cor para a UI):
  Alugados 2, Aguardando Devolução 8, Reservado 3, Uso Interno 5, Emprestado 4,
  Manutenção 6, Sem Conserto 14, Garantia 7, Liberado Para Locação 1, Baixado 9.
- `qtd` é a contagem de equipamentos ativos naquele `situacao_id` (0 quando não
  há nenhum).

## 5. `GET /api/consulta/tombos`

Lista **um item por tombo** (`numero`), com agregados de todas as cópias.

Query string (todas opcionais, combináveis):

| parâmetro | regra |
| --- | --- |
| `numero` | tombo exato |
| `orgao_id` | `ORG_ID` exato |
| `tipo_id` | `aluguel_tipos.id` exato |
| `situacao_id` | `situacao_id` do cadastro (é o que os chips filtram) |
| `descricao` | `LIKE %texto%` em `descricao` **ou** `nome` |
| `somente_duplicados` | `1/true/sim/s` → só tombos com mais de uma cópia ativa |
| `page` / `per_page` | padrão 1 / 24; `per_page` máximo 100 |

Os filtros decidem **quais** tombos aparecem (basta uma cópia ativa satisfazer).
Os contadores do card sempre somam **todas** as cópias do tombo, inclusive as
excluídas.

```json
{
  "total": 91104, "page": 1, "per_page": 24, "paginas": 3796,
  "ultimo_equipamento": 106611,
  "filtros": {"numero": null, "orgao_id": null, "tipo_id": null,
              "situacao_id": null, "descricao": null,
              "somente_duplicados": false},
  "itens": [{
    "numero": 44219, "copias": 3, "ativas": 3, "excluidas": 0,
    "duplicado": true, "alugados": 1,
    "descricao": "MICRO I5, 4GB RAM, SSD 120GB NS ...",
    "nome": "MICRO CORE I5 4GB HD 500",
    "orgaos": ["ALUCOM", "TONER ADI"],
    "tipos": ["MICRO I5", "IMPRESSORA BROTHER DCP B-7535DW"],
    "situacoes": [
      {"situacao_id": 7, "situacao": "NA GARANTIA", "qtd": 2},
      {"situacao_id": 1, "situacao": "LIBERADO P/ LOCACAO", "qtd": 1}
    ]
  }]
}
```

- `copias` = total de equipamentos com o `numero`, ativos ou excluídos.
- `ativas`/`excluidas` = divisão por `deleted_at`.
- `duplicado` = `copias > 1`.
- `alugados` = cópias ativas cujo último movimento relevante é de aluguel.
- `orgaos`, `tipos`, `situacoes` são derivados apenas das cópias ativas.
- `page` é ajustado para a última página quando o valor pedido passa do fim.

## 6. `GET /api/consulta/tombos/<numero>`

Todas as cópias de um tombo, com o estado atual de cada uma.

```json
{
  "numero": 44219, "copias_total": 3, "ativas": 3, "excluidas": 0,
  "orgaos": ["ALUCOM", "TONER ADI"],
  "situacoes": [{"situacao_id": 7, "situacao": "NA GARANTIA", "qtd": 2}],
  "itens": [{
    "equipment_id": 54089, "numero": 44219, "numero_antigo": null,
    "nome": "MICRO CORE I5 4GB HD 500", "descricao": "MICRO I5, 4GB RAM...",
    "observacoes": "3* GERAÇÃO",
    "tipo_id": 1947, "tipo": "MICRO I5",
    "orgao_id": 1115, "orgao": "ALUCOM", "uso_interno": false,
    "situacao_id": 7, "situacao": "NA GARANTIA",
    "alugado": false, "cliente": "", "cliente_desde": "",
    "movimento_id": 555702, "movimentos_total": 11,
    "data_locacao": "", "data_garantia": "27/08/2022",
    "data_aquisicao": "", "deleted_at": "", "ativo": true,
    "situacao_divergente": false
  }]
}
```

- `observacoes` é a coluna `acessorio` do cadastro (o que a tela antiga chama
  de Observações).
- `movimentos_total` é o total real de itens de movimento da cópia.
- `cliente` vem como `"<cliente_id> - <nome_razao_social>"` e só aparece quando
  `alugado` é `true`; `cliente_desde` é a data desse aluguel.
- `copias_total` inclui excluídas; `situacoes`/`orgaos` consideram só ativas.
- 404 `tombo_inexistente` quando nenhum equipamento tem o número.

## 7. `GET /api/consulta/equipamentos/<equipamento_id>`

Histórico completo de uma cópia.

```json
{
  "equipamento": {
    "equipment_id": 118545, "numero": 44219, "numero_antigo": null,
    "nome": "...", "descricao": "...", "observacoes": "...",
    "uso_interno": false, "situacao_id": 1, "situacao": "LIBERADO P/ LOCACAO",
    "orgao_id": 1115, "orgao": "ALUCOM", "tipo_id": 1947, "tipo": "MICRO I5",
    "data_locacao": "", "data_garantia": "", "data_aquisicao": "",
    "deleted_at": "", "ativo": true, "situacao_divergente": true
  },
  "alugado": true, "cliente_atual": "3084 - P.M DE CAUCAIA - ...",
  "cliente_desde": "13/07/2026 09:56", "ultima_movimentacao": 556875,
  "copias_total": 3,
  "copias": [{
    "equipment_id": 54089, "numero": 44219, "nome": "...",
    "situacao_id": 7, "situacao": "NA GARANTIA", "orgao": "ALUCOM",
    "alugado": false, "cliente": "", "deleted_at": "", "atual": true
  }],
  "movimentos_total": 12,
  "movimentos": [/* shape da §8 */],
  "atrelados": [{
    "equipment_id": 132695, "numero": 54049, "nome": "...",
    "situacao_id": 1, "situacao": "LIBERADO P/ LOCACAO", "orgao": "ALUCOM",
    "data": "22/06/2026 11:37", "cliente": "2341 - ...", "observacao": "",
    "deleted_at": ""
  }]
}
```

- `copias` lista todos os equipamentos com o mesmo `numero`, ativos ou não (até
  100); `atual: true` marca a cópia aberta.
- `movimentos_total` é o total real no legado; `movimentos` devolve no máximo
  200, mais recentes primeiro.
- `atrelados` são os **equipamentos antigos** ligados a esta cópia por
  `aluguel_substituicao`: para cada par em que o equipamento está no lado
  `substituicao_aluguel_id`, devolve os itens do lado
  `substituicao_devolucao_id`. (A tabela `related_equipments` está vazia no
  legado; não é usada.)
- 404 `equipamento_inexistente` quando o id não existe.

### 7.1 Item de movimento (dentro de `movimentos`)

```json
{
  "movimento_id": 555702, "numero": 555702, "tipo": "SD", "tipo_id": 3,
  "tipo_nome": "Substituição Devolução", "data": "22/06/2026 11:37",
  "cliente_id": 2341, "cliente": "2341 - ...", "orgao": "ALUCOM",
  "status": "Ativo", "ativo": true,
  "movimento": "SA - 555703", "par_id": 555703,
  "tem_detalhe": true, "observacoes": "", "declaracao": ""
}
```

- `numero` é o id do movimento (é a coluna "Número" da tela antiga).
- `movimento` é o par da substituição no formato `"<tipo do par> - <id do par>"`
  quando existe par em `aluguel_substituicao`; senão `"-"`.
- `status` é `Ativo`/`Excluído` por `deleted_at` do movimento.
- `tem_detalhe` (o "olho" da tela antiga) é `true` **somente** em
  `tipo_id IN (2,3)` — Devolução e Substituição Devolução.

## 8. `GET /api/consulta/movimentos/<movimento_id>`

```json
{
  "movimento": {
    "id": 556875, "numero": 556873, "tipo": "SA", "tipo_id": 5,
    "tipo_nome": "Substituição Alugado", "data": "13/07/2026 09:56",
    "cliente_id": 3084, "cliente": "3084 - P.M DE CAUCAIA - ...",
    "orgao": "...", "status": "Ativo", "observacoes": "", "declaracao": "",
    "par": {"movimento_id": 556874, "tipo": "SD", "numero": 556873,
            "data": "13/07/2026 09:56", "cliente": "..."}
  },
  "itens": [{"equipment_id": 118545, "numero": 44219, "nome": "...",
             "orgao_anterior": "", "orgao_novo": ""}]
}
```

`par` é `null` quando o movimento não participa de substituição. 404
`movimento_inexistente` para id desconhecido.

## 9. Regras de leitura do legado

- `alugado`, `cliente` e `cliente_desde` vêm do **último movimento relevante**
  (`tipo_id IN (1,2,3,4,5,6,8)`): alugado quando esse movimento é
  `Alugado (1)` ou `Substituição Alugado (5)`.
- `situacao`/`situacao_id` são o **cadastro**, que é defasado no legado.
  `situacao_divergente: true` avisa que cadastro e movimentos discordam (ex.:
  cadastro `LIBERADO P/ LOCACAO` com aluguel em curso). A UI mostra o cadastro
  e sinaliza a divergência, sem corrigir nada.
- `numero` é o tombo; nunca é alterado. `equipment_id` é o id da cópia no
  legado.
- A UI não recalcula agregados: usa exatamente os contadores do backend.

## 10. Mock da consulta (`?mock=1`)

Com `?mock=1` a UI não chama a API real; usa estas fixtures em `static/mock/`:

| rota | fixture |
| --- | --- |
| `/api/consulta/filtros` | `consulta_filtros.json` |
| `/api/consulta/tombos` | `consulta_tombos.json` |
| `/api/consulta/tombos/44219` | `consulta_tombo_44219.json` |
| `/api/consulta/equipamentos/54089` | `consulta_detalhe_54089.json` |
| `/api/consulta/equipamentos/118545` | `consulta_detalhe_118545.json` |
| `/api/consulta/movimentos/556875` | `consulta_movimento_556875.json` |

- `consulta_tombos.json` é uma página real de 24 cards que inclui o tombo
  44219 (3 cópias, 1 alugada).
- `consulta_detalhe_54089.json` tem 11 movimentos (pares SD/SA incluídos),
  2 atrelados, `copias_total: 3` e `observacoes: "3* GERAÇÃO"`.
- `consulta_detalhe_118545.json` é a outra cópia do 44219: alugada,
  `situacao_divergente` visível, sem atrelados.
- O roteador de mock deve casar por caminho, ignorar a query string e
  **preservar `mock=1`** em toda URL que a UI escrever.
- O botão "olho" abre `consulta_movimento_556875.json`; ids de equipamento sem
  fixture caem em `consulta_detalhe_54089.json`.

## 11. Onde os arquivos vivem

```
front/
  app.py                # criação do app Flask + rotas (consulta-only)
  servico_consulta.py   # consultas read-only no aluguel_legado
  CONTRATO_API.md
  templates/consulta.html
  static/consulta.css
  static/consulta.js
  static/mock/*.json
```

O pipeline de ajuste (`ajustes/duplicatas_equipamentos`) não é importado pelo
front e continua sendo executado apenas pela CLI.
