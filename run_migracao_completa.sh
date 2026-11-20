#!/usr/bin/env bash
# Migração completa — reset, cadastros, movimentos, ajustes, delta de produção,
# status dos equipamentos e REQUISIÇÕES 2026 (com protocolo numerado).
#
# Sem dump/restore: o pedido é executar a migração. A conferência final fica
# em verificar_requisicoes_2026.py (dentro do repo).
#
# Uso: ./run_migracao_completa.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

TS=$(date +%Y%m%d_%H%M)
LOG=/tmp/opencode/migracao_full_${TS}.log
VENV=.venv/bin/python
OUT_DELTA=ajustes/delta_apply_${TS}
LEDGER=ajustes/delta_ledger_${TS}.csv
export PYTHONUNBUFFERED=1

ln -sfn "$LOG" /tmp/opencode/migracao_atual.log

if pgrep -af "[m]ain.py|[i]mportar_delta_movimentos.py" >/dev/null; then
  echo "Já existe execução em andamento. Abortando." >&2
  exit 2
fi

{
  echo "INICIO $(date '+%F %T') log=$LOG"
  echo "###### 1/9 RESET_BANCO ######"
  timeout 1800 "$VENV" main.py reset_banco

  echo "###### 2/9 TODOS (cadastros + parquets + movimentos + ajustes) ######"
  timeout 21600 "$VENV" main.py todos

  echo "###### 3/9 AJUSTES_MOVIMENTOS --aplicar ######"
  timeout 7200 "$VENV" main.py ajustes_movimentos --aplicar

  echo "###### 4/9 EXTRAIR DELTA PRODUCAO ######"
  timeout 3600 "$VENV" /tmp/opencode/extrair_delta_prod.py

  echo "###### 5/9 APLICAR DELTA ######"
  timeout 3600 "$VENV" importar_delta_movimentos.py \
    --json /tmp/opencode/delta_prod_04_12_11.json \
    --out-dir "$OUT_DELTA" \
    --ledger "$LEDGER" \
    --apply

  echo "###### 6/9 STATUS DOS EQUIPAMENTOS (04-12/11) ######"
  timeout 1800 "$VENV" atualizar_status_equipamentos.py --apply

  echo "###### 7/9 VALIDAR MESCLAGENS ######"
  timeout 900 "$VENV" /tmp/opencode/validar_mesclagens.py

  echo "###### 8/9 REQUISICOES 2026 --execute ######"
  timeout 1800 "$VENV" main.py requisicoes --execute

  echo "###### 9/9 VERIFICAR REQUISICOES ######"
  timeout 900 "$VENV" verificar_requisicoes_2026.py

  echo "FIM $(date '+%F %T')"
} > "$LOG" 2>&1

echo "LOG=$LOG"
