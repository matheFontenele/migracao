#!/usr/bin/env bash
# Migracao completa 18/11-C.
#
# Mesma esteira da 18/11-B (reset -> cadastros -> movimentos -> ajustes ->
# delta de producao -> validacao) com o passo final de REQUISICOES 2026
# (`main.py requisicoes --execute`), seguido de dump e restore de conferencia.
#
# Uso: ./run_full_migration.sh [dump_de_saida]
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

TS=$(date +%Y%m%d_%H%M)
LOG=/tmp/opencode/migracao_full_${TS}.log
DUMP=${1:-/home/matheus/Documentos/dumps_refatorado/dump_18_11c_com_delta_e_requisicoes.sql}
VENV=.venv/bin/python
export PYTHONUNBUFFERED=1

ln -sfn "$LOG" /tmp/opencode/migracao_atual.log

if pgrep -af "[m]ain.py|[i]mportar_delta_movimentos.py" >/dev/null; then
  echo "Ja existe execucao em andamento. Abortando." >&2
  exit 2
fi

verificar_requisicoes() {
  "$VENV" /tmp/opencode/verificar_requisicoes_2026.py
}

{
  echo "INICIO $(date '+%F %T') log=$LOG"
  echo "###### 1/11 RESET_BANCO ######"
  timeout 1800 "$VENV" main.py reset_banco

  echo "###### 2/11 TODOS (cadastros + movimentos + ajustes) ######"
  timeout 21600 "$VENV" main.py todos

  echo "###### 3/11 AJUSTES_MOVIMENTOS --aplicar ######"
  timeout 7200 "$VENV" main.py ajustes_movimentos --aplicar

  echo "###### 4/11 EXTRAIR DELTA PRODUCAO ######"
  timeout 3600 "$VENV" /tmp/opencode/extrair_delta_prod.py

  echo "###### 5/11 APLICAR DELTA ######"
  timeout 3600 "$VENV" importar_delta_movimentos.py \
    --json /tmp/opencode/delta_prod_04_12_11.json \
    --out-dir ajustes/delta_apply_18_11c \
    --ledger ajustes/delta_ledger_18_11c.csv \
    --apply

  echo "###### 6/11 VALIDAR MESCLAGENS ######"
  timeout 900 "$VENV" /tmp/opencode/validar_mesclagens.py

  echo "###### 7/11 REQUISICOES 2026 --execute ######"
  timeout 1800 "$VENV" main.py requisicoes --execute

  echo "###### 8/11 VERIFICAR REQUISICOES ######"
  verificar_requisicoes

  echo "###### 9/11 GERAR DUMP ######"
  timeout 1800 /tmp/opencode/gerar_dump_91.sh "$DUMP"

  echo "###### 10/11 RESTORE DE CONFERENCIA ######"
  timeout 3600 /tmp/opencode/restaurar_dump_18.sh "$DUMP"

  echo "###### 11/11 VERIFICAR DUMP ######"
  timeout 1800 "$VENV" /tmp/opencode/verificar_dump_18.py
  mysql --host=127.0.0.1 --port=3307 --user=root --password=root \
    -e "DROP DATABASE IF EXISTS merge_verify;" 2>/dev/null
  echo "merge_verify removida"

  echo "FIM $(date '+%F %T')"
} > "$LOG" 2>&1

echo "LOG=$LOG"
echo "DUMP=$DUMP"
