#!/usr/bin/env bash
# Acompanha o log da esteira de migracao em tempo real.
#
# O proprio run_migracao_completa.sh aponta /tmp/opencode/migracao_atual.log
# para o log da execucao corrente (symlink recriado a cada rodada).
#
# Uso:
#   ./ver_log.sh            # segue o log filtrando etapas e marcos (padrao)
#   ./ver_log.sh bruto      # segue tudo, incluindo barras de progresso
#   ./ver_log.sh atual      # etapa em andamento + ultimas linhas (sem -f)
#   ./ver_log.sh ultimo     # ultimas 60 linhas uteis (sem -f)
#   LOG=/caminho/arquivo ./ver_log.sh bruto
set -uo pipefail

LOG=${LOG:-/tmp/opencode/migracao_atual.log}

esperar_log() {
  if [ ! -e "$LOG" ]; then
    echo "aguardando $LOG aparecer..." >&2
    until [ -e "$LOG" ]; do sleep 1; done
  fi
}

case "${1:-etapas}" in
  bruto)
    esperar_log
    # As barras do tqdm usam \r; sem isso a saida vira uma linha so.
    tail -f "$LOG" | tr '\r' '\n'
    ;;
  etapas)
    esperar_log
    tail -f "$LOG" | tr '\r' '\n' | grep --line-buffered -aE \
      "^######|INICIO |FIM |✅|❌|⚠️|ERRO|Traceback|ABORTOU|TOTAL |Resultado:|PIPELINE"
    ;;
  atual)
    esperar_log
    echo "== processo =="
    pgrep -af "[r]un_migracao_completa|[m]ain\.py|[i]mportar_delta|[e]xtrair_delta" \
      || echo "nenhum processo da esteira rodando"
    echo "== ultima etapa =="
    grep -a "######" "$LOG" | tail -1
    echo "== ultimas linhas =="
    tail -c 400 "$LOG" | tr '\r' '\n' | tail -3
    ;;
  ultimo)
    esperar_log
    tail -n 200 "$LOG" | tr '\r' '\n' | grep -av '^$' | tail -60
    ;;
  *)
    echo "uso: $0 [etapas|bruto|atual|ultimo]" >&2
    exit 2
    ;;
esac
