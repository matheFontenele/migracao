import os
import sys
from migracao_movimentos import (
    carregar_dados_compartilhados,
    executar_truncate_tabelas,
    resetar_saldo_contract_items
)

from migracao_aluguel import MigracaoAluguel
from migracao_reserva import MigracaoReserva
# from migracao_devolucao import MigracaoDevolucao
# from migracao_substituicao import MigracaoSubstituicao

TABELAS = []
# ==============================================================================
# ORQUESTRADOR PRINCIPAL
# ==============================================================================
class OrquestradorMovimentos:

    def __init__(self, engine_new, engine_legado):
        self.engine_new = engine_new
        self.engine_legado = engine_legado

    def executar(self):
        print("\n" + "=" * 80)
        print("🚀 INICIANDO ORQUESTRAÇÃO DE MOVIMENTOS (POO)")
        print("=" * 80)

        try:
            # 1. Reset e limpeza antes de qualquer migração
            print("\n🧹 Executando faxina e reset de saldos (Contract Items)...")
            resetar_saldo_contract_items(self.engine_new)
            executar_truncate_tabelas(self.engine_new, TABELAS)
            print("   ✅ Limpeza de movimentos concluída.")

            # 2. Carrega dados compartilhados na RAM uma única vez
            print("\n🧠 Carregando dados compartilhados na RAM (Caches)...")
            dados_compartilhados = carregar_dados_compartilhados(self.engine_legado, self.engine_new)

            # 3. Execução Isolada por Módulo (Injetando a RAM)
            print("\n▶️ Iniciando Módulo: ALUGUEL")
            aluguel = MigracaoAluguel(self.engine_new, self.engine_legado, dados_compartilhados, start_counter=1)
            aluguel.executar()

            print("\n▶️ Iniciando Módulo: RESERVA")
            reserva = MigracaoReserva(self.engine_new, self.engine_legado, dados_compartilhados, start_counter=500000)
            reserva.executar()

            # print("\n▶️ Iniciando Módulo: DEVOLUÇÃO")
            # devolucao = MigracaoDevolucao(self.engine_new, self.engine_legado, dados_compartilhados, start_counter=1000000)
            # devolucao.executar()

            print("\n" + "=" * 80)
            print("🎉 PIPELINE DE MOVIMENTOS CONCLUÍDO COM SUCESSO")
            print("=" * 80)

        except Exception as e:
            print(f"\n❌ ERRO CRÍTICO NO ORQUESTRADOR DE MOVIMENTOS: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)


# ==============================================================================
# WRAPPER (Ponte para a execução dinâmica do main.py)
# ==============================================================================
def executar(eng_novo, eng_legado):
    orquestrador = OrquestradorMovimentos(eng_novo, eng_legado)
    orquestrador.executar()