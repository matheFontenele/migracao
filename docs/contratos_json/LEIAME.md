# Contratos em JSON — interface com a migração

A migração **não lê mais planilhas**. Ela carrega o que estiver aqui.
Um arquivo = um contrato.

```
ALUCOM/            <numero>.json
MOREIA/            <numero>.json
IP/                <numero>.json
AS_SISTEMAS/       <numero>.json
_novos/<ORG>/      contrato que ainda não existe no banco (exige aprovação)
_pendentes/<ORG>/  rascunho com pendência bloqueante (bloqueia a carga)
_referencia/       customers.csv, organizations.csv, enums.json, snapshots
relatorios/        cobertura, pendências, avisos, revisão humana
manifest.json      sha256 e contagens de cada arquivo
```

## Regras que a migração aplica

- `schema_version` = 1.
- `organization.cnpj` tem que ser o da pasta (ALUCOM `01628251000188`,
  MOREIA `03420933000126`, IP `31810764000176`, AS `63320667000191`).
- Só arquivos com `contract.status = "OK"` na pasta da organização.
  `PENDENTE` vai para `_pendentes/`; contrato novo vai para `_novos/`.
- `stable_key` única em todo o conjunto; `number` + `name` são a identidade
  visível (o `id` do banco é gerado na carga).
- `customer_alias` e `recipient_aliases` são **alias de customers**, nunca id.
- `items[].alias` ≤ 255; `lineage_key` obrigatório e igual entre aditivos da
  mesma linhagem (o `root_item_id` sai daí).
- `occurrence` pode repetir quando a fonte tem a mesma linha duas vezes no
  mesmo aditivo — nesse caso as duas linhas compartilham a linhagem.
- `info` pode ser `null` quando a fonte não declara vigência; nunca invente
  datas. Quando existe, `start_date`, `duration`, `max_duration` e
  `total_amount` são obrigatórios.
- Nome de arquivo repetido dentro da organização leva sufixo `__id<origem>`.

## Como rodar

```bash
# carrega o que está em docs/contratos_json
.venv/bin/python main.py contratos

# gera/atualiza o JSON a partir do banco (bootstrap e conferência)
.venv/bin/python main.py exportar_contratos
```

A carga valida tudo antes de truncar: JSON inválido, cliente inexistente ou
redução brusca de contratos abortam sem apagar nada.

## Relatórios

`relatorios/pendencias.csv` é a fila de revisão humana. Cada linha traz
arquivo, local, texto original e ação esperada. Respostas ficam em
`_referencia/decisoes_humanas.csv`.
