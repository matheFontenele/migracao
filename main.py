import argparse
import sys
import os

from cadastros.contratos import migracao_contratantes
from config.database import obter_engines

# 1. IMPORTAÇÃO DOS MÓDULOS COMO PACOTES PYTHON
from cadastros.clientes import migracao_cliente
from cadastros.contratos import migracao_contratos
from cadastros.equipamentos import migracao_equipamentos
from movimentos import orquestrador_movimentos, migracao_aluguel


# 2. O MAPEAMENTO AGORA APONTA PARA FUNÇÕES REAIS, NÃO PARA TEXTOS!
TAREFAS = {
    "clientes": migracao_cliente.executar,
    "contratos": migracao_contratos.executar,
    "contratantes": migracao_contratantes.executar,
    "equipamentos": migracao_equipamentos.executar,
    "movimentos": orquestrador_movimentos.executar,
    "movimentos_aluguel": migracao_aluguel.executar
}


def despachar_tarefa(nome_tarefa, eng_novo, eng_legado):
    funcao_alvo = TAREFAS[nome_tarefa]

    print(f"\n🚀 EXECUTANDO: {nome_tarefa.upper()}...")
    try:
        # A magia acontece aqui: injetamos as engines direto na veia da função
        funcao_alvo(eng_novo, eng_legado)
        print(f"✅ {nome_tarefa.upper()} concluído com sucesso.")
        
    except Exception as e:
        print(f"\n❌ {nome_tarefa.upper()} ABORTOU COM ERRO CRÍTICO: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Pipeline de Migração Orquestrado")
    parser.add_argument(
        "alvo", nargs="?", default="todos",
        help="Opções: clientes, contratos, contratantes, equipamentos, movimentos, movimentos_aluguel ou todos"
    )

    args = parser.parse_args()
    alvo = args.alvo.lower().strip()

    if alvo not in TAREFAS and alvo != "todos":
        print(f"❌ Erro: Alvo '{alvo}' não reconhecido pelo sistema.")
        print("\nComandos válidos:")
        print("  python main.py todos                (Roda a esteira inteira)")
        print("  python main.py clientes             (Apenas tabela de Clientes)")
        print("  python main.py movimentos_aluguel   (Apenas Aluguel isolado)")
        sys.exit(1)

    # Liga a usina uma única vez para toda a vida do comando:
    print("🔌 Orquestrador: Estabelecendo conexões com os bancos...")
    eng_novo, eng_legado = obter_engines()

    # Esteira Sequencial (Respeitando as Chaves Estrangeiras do MySQL)
    if alvo == "todos":
        print("⚡ Disparando migração em lote...")
        ordem_sre = ["clientes", "contratos", "contratantes", "equipamentos", "movimentos"]
        
        for etapa in ordem_sre:
            despachar_tarefa(etapa, eng_novo, eng_legado)
            
        print("\n🏆 PIPELINE COMPLETO FINALIZADO COM 100% DE INTEGRIDADE!")

    # Execução Granular Isolada
    else:
        despachar_tarefa(alvo, eng_novo, eng_legado)


if __name__ == "__main__":
    main()