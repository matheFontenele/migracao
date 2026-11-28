#!/usr/bin/env bash
# Migração completa do zero — reset, cadastros, movimentos, ajustes, delta de
# produção, status dos equipamentos, REQUISIÇÕES 2026 e conferência final.
#
# Sem dump/restore: a fonte é o snapshot `aluguel_legado` (< 05/10/2026) e o
# delta vem de `prod_14_11` (04 a 12/11/2026), não do dump novo.
#
# Passo `parquets` de propósito FORA da esteira: `_publicar` moveria o
# consolidado de `docs/parquets` para `_anterior/` e publicaria os arquivos de
# `locks_parquet`, mudando a base de movimentos. `movimentos` consome o
# parquet consolidado que já está em `docs/parquets`.
#
# `ajustes/duplicatas_equipamentos/` também fica fora: 03_aplicar só escreve
# grupos aprovados manualmente (mapa_canonico.csv sem aprovações) e a
# consolidação precisa de revisão humana antes de rodar com --apply.
#
# Uso:
#   ./run_migracao_completa.sh              # run completo (etapas 1..9)
#   ./run_migracao_completa.sh --ate 2      # só reset_banco + cadastros
#   ./run_migracao_completa.sh --de 3       # retoma da etapa 3 (movimentos)
#
# Log: /tmp/opencode/migracao_atual.log (symlink para o log do run)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

DE=1
ATE=9
while [ $# -gt 0 ]; do
  case "$1" in
    --de)  DE="${2:?--de exige um número}"; shift 2 ;;
    --ate) ATE="${2:?--ate exige um número}"; shift 2 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "Opção inválida: $1 (use --de N / --ate N)" >&2; exit 2 ;;
  esac
done

if ! [[ "$DE" =~ ^[1-9]$ && "$ATE" =~ ^[1-9]$ ]] || [ "$DE" -gt "$ATE" ]; then
  echo "--de e --ate devem ser de 1 a 9 e --de <= --ate (recebido --de $DE --ate $ATE)" >&2
  exit 2
fi

# etapa N -> verdadeiro quando N está dentro da faixa --de..--ate
etapa() { [ "$DE" -le "$1" ] && [ "$ATE" -ge "$1" ]; }

TS=$(date +%Y%m%d_%H%M)
LOG=/tmp/opencode/migracao_full_${TS}.log
VENV=.venv/bin/python
JSON=/tmp/opencode/delta_prod_04_12_11.json
REL=/tmp/opencode/delta_extracao.csv
OUT_DELTA=ajustes/delta_apply_${TS}
LEDGER=ajustes/delta_ledger_${TS}.csv
INICIO=2026-11-04
FIM=2026-11-12
export PYTHONUNBUFFERED=1

mkdir -p /tmp/opencode
ln -sfn "$LOG" /tmp/opencode/migracao_atual.log

if pgrep -af "[m]ain.py|[i]mportar_delta_movimentos.py|[a]tualizar_status_equipamentos.py" >/dev/null; then
  echo "Já existe execução em andamento. Abortando." >&2
  exit 2
fi

trap 'echo "❌ FALHA em $(date "+%F %T") (linha $LINENO)"' ERR

{
  echo "INICIO $(date '+%F %T') etapas=${DE}..${ATE} log=$LOG"
  echo "json=$JSON out_delta=$OUT_DELTA ledger=$LEDGER"

  if etapa 1; then
    echo "###### 1/9 RESET_BANCO ######"
    timeout 1800 "$VENV" main.py reset_banco
  fi

  if etapa 2; then
    echo "###### 2/9 CADASTROS (clientes, contratos, contratantes, equipamentos, insumos) ######"
    timeout 10800 "$VENV" main.py cadastros --ignorar-pendentes --incluir-novos
  fi

  if etapa 3; then
    echo "###### 3/9 MOVIMENTOS (aluguel, substituicao, reserva, devolucao, manutencao, datas, status, estoque) ######"
    timeout 21600 "$VENV" main.py movimentos
  fi

  if etapa 4; then
    echo "###### 4/9 AJUSTES_MOVIMENTOS --aplicar ######"
    timeout 7200 "$VENV" main.py ajustes_movimentos --aplicar
  fi

  if etapa 5; then
    echo "###### 5/9 EXTRAIR DELTA PRODUCAO $INICIO..$FIM ######"
    timeout 3600 "$VENV" extrair_delta_prod.py \
      --inicio "$INICIO" --fim "$FIM" --out "$JSON" --relatorio "$REL"
  fi

  if etapa 6; then
    echo "###### 6/9 APLICAR DELTA ######"
    timeout 3600 "$VENV" importar_delta_movimentos.py \
      --json "$JSON" --inicio "$INICIO" --fim "$FIM" \
      --out-dir "$OUT_DELTA" --ledger "$LEDGER" --apply
  fi

  if etapa 7; then
    echo "###### 7/9 STATUS DOS EQUIPAMENTOS ($INICIO..$FIM) ######"
    timeout 1800 "$VENV" atualizar_status_equipamentos.py \
      --json "$JSON" --inicio "$INICIO" --fim "$FIM" --apply
  fi

  if etapa 8; then
    echo "###### 8/9 REQUISICOES 2026 --execute ######"
    timeout 1800 "$VENV" main.py requisicoes --execute
  fi

  if etapa 9; then
    echo "###### 9/9 VERIFICAR REQUISICOES 2026 ######"
    timeout 900 "$VENV" verificar_requisicoes_2026.py
  fi

  if etapa 6; then
    echo "###### RESUMO DO DELTA APLICADO ######"
    "$VENV" - "$LEDGER" <<'PY'
import csv, sys
from collections import Counter

caminho = sys.argv[1]
with open(caminho, encoding="utf-8-sig") as handle:
    linhas = list(csv.DictReader(handle))
total = Counter(r["action"] for r in linhas)
print(f"linhas no ledger: {len(linhas)} {dict(total)}")
por_tabela = {}
for r in linhas:
    por_tabela.setdefault(r["table"], Counter())[r["action"]] += 1
bloqueados = [r for r in linhas if r["action"] == "blocked"]
for tabela in sorted(por_tabela):
    print(f"  {tabela:24} {dict(por_tabela[tabela])}")
print(f"bloqueados: {len(bloqueados)}")
PY
  fi

  echo "FIM $(date '+%F %T')"
} > "$LOG" 2>&1

echo "LOG=$LOG"
echo "Acompanhe em outro terminal com: tail -f /tmp/opencode/migracao_atual.log"
