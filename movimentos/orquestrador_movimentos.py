import os
from sqlalchemy import create_engine
from migracao_movimentos import (
    carregar_dados_compartilhados,
    limpar_tabelas_refatoradas,
    resetar_saldo_contract_items
)

from migracao_aluguel import MigracaoAluguel
from migracao_reserva import MigracaoReserva
# from migracao_devolucao import MigracaoDevolucao
# from migracao_substituicao import MigracaoSubstituicao

# ==============================================================================
# CONFIGURAÇÃO DOS BANCOS DE DADOS
# ==============================================================================
DB_CONFIG_NEW = {
    "host": "localhost", "port": "3307", "db": "controle-interno", "user": "root", "pass": "root"
}
DB_CONFIG_LEGADO = {
    "host": "172.16.0.200", "port": "3310", "db": "aluguel_legado", "user": "root", "pass": "1234"
}

engine_new = create_engine(
    f"mysql+pymysql://{DB_CONFIG_NEW['user']}:{DB_CONFIG_NEW['pass']}@{DB_CONFIG_NEW['host']}:{DB_CONFIG_NEW['port']}/{DB_CONFIG_NEW['db']}"
)
engine_legado = create_engine(
    f"mysql+pymysql://{DB_CONFIG_LEGADO['user']}:{DB_CONFIG_LEGADO['pass']}@{DB_CONFIG_LEGADO['host']}:{DB_CONFIG_LEGADO['port']}/{DB_CONFIG_LEGADO['db']}"
)

# ==============================================================================
# ORQUESTRADOR PRINCIPAL
# ==============================================================================
def main():
    print("=" * 70)
    print("🚀 INICIANDO ORQUESTRAÇÃO DE MOVIMENTOS (POO)")
    print("=" * 70)

    # 1. Reset e limpeza antes de qualquer migração
    resetar_saldo_contract_items(engine_new)
    limpar_tabelas_refatoradas(engine_new)

    # 2. Carrega dados compartilhados na RAM uma única vez
    dados_compartilhados = carregar_dados_compartilhados(engine_legado, engine_new)

    # 3. Execução Isolada por Módulo (Passando a RAM compartilhada)
    print("\n▶️ Iniciando Módulo: ALUGUEL")
    aluguel = MigracaoAluguel(engine_new, engine_legado, dados_compartilhados, start_counter=1)
    aluguel.executar()

    print("\n▶️ Iniciando Módulo: RESERVA")
    reserva = MigracaoReserva(engine_new, engine_legado, dados_compartilhados, start_counter=500000)
    reserva.executar()

    # print("\n▶️ Iniciando Módulo: DEVOLUÇÃO")
    # devolucao = MigracaoDevolucao(engine_new, engine_legado, dados_compartilhados, start_counter=1000000)
    # devolucao.executar()

    print("\n" + "=" * 70)
    print("🎉 PIPELINE DE MOVIMENTOS CONCLUÍDO")
    print("=" * 70)


if __name__ == "__main__":
    main()