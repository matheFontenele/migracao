import argparse
import sys
import os

from config.database import obter_engines

# ==============================================================================
# 1. WRAPPERS COM LAZY LOADING (Importação Tardia)
# ==============================================================================

def iniciar_clientes(eng_novo, eng_legado):
    from cadastros.clientes.migracao_cliente import MigracaoClientes
    migrador = MigracaoClientes(eng_novo, eng_legado)
    migrador.executar()

def iniciar_contratos(eng_novo, eng_legado):
    from cadastros.contratos import migracao_contratos
    migracao_contratos.executar(eng_novo, eng_legado)

def iniciar_contratantes(eng_novo, eng_legado):
    from cadastros.contratos import migracao_contratantes
    migracao_contratantes.executar(eng_novo, eng_legado)

def iniciar_equipamentos(eng_novo, eng_legado):
    from cadastros.equipamentos import migracao_equipamentos
    migracao_equipamentos.executar(eng_novo, eng_legado)

def iniciar_aluguel(eng_novo, eng_legado):
    from movimentos import migracao_aluguel
    migracao_aluguel.executar(eng_novo, eng_legado)

def iniciar_movimentos(eng_novo, eng_legado):
    from movimentos import orquestrador_movimentos
    orquestrador_movimentos.executar(eng_novo, eng_legado)

# ==============================================================================
# 2. MAPEAMENTO DE TAREFAS E GRUPOS
# ==============================================================================
TAREFAS = {
    "clientes": iniciar_clientes,
    "contratos": iniciar_contratos,
    "contratantes": iniciar_contratantes,
    "equipamentos": iniciar_equipamentos,
    "movimentos_aluguel": iniciar_aluguel,
    "movimentos": iniciar_movimentos
}

GRUPOS = {
    "cadastros": ["clientes", "contratos", "contratantes", "equipamentos"],
    "todos": ["clientes", "contratos", "contratantes", "equipamentos", "movimentos"]
}

def despachar_tarefa(nome_tarefa, eng_novo, eng_legado):
    funcao_alvo = TAREFAS[nome_tarefa]

    print(f"\n🚀 EXECUTANDO: {nome_tarefa.upper()}...")
    try:
        # A mágica acontece aqui: injetamos as engines direto na veia da função
        funcao_alvo(eng_novo, eng_legado)
        print(f"✅ {nome_tarefa.upper()} concluído com sucesso.")
        
    except Exception as e:
        print(f"\n❌ {nome_tarefa.upper()} ABORTOU COM ERRO CRÍTICO: {e}")
        import traceback
        traceback.print_exc()  # Ajuda muito no debug local para ver onde a classe quebrou!
        sys.exit(1)

# ==============================================================================
# 3. INTERFACE DE LINHA DE COMANDO (CLI)
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Pipeline de Migração Orquestrado")
    parser.add_argument(
        "alvo", nargs="?", default="todos",
        help="Opções: cadastros, todos OU módulos individuais (clientes, contratos, equipamentos...)"
    )

    args = parser.parse_args()
    alvo = args.alvo.lower().strip()

    # Validação do Argumento
    if alvo not in TAREFAS and alvo not in GRUPOS:
        print(f"❌ Erro: Alvo '{alvo}' não reconhecido pelo sistema.\n")
        print("Comandos válidos para Lote:")
        print("  python main.py todos        (Roda a esteira inteira)")
        print("  python main.py cadastros    (Roda apenas clientes, contratos, contratantes, equipamentos)")
        print("\nComandos válidos para Debug Individual:")
        for t in TAREFAS.keys():
            print(f"  python main.py {t}")
        sys.exit(1)

    # Liga a usina uma única vez para toda a vida do comando:
    print("🔌 Orquestrador: Estabelecendo conexões com os bancos...")
    eng_novo, eng_legado = obter_engines()

    # Se o alvo for um grupo (ex: 'cadastros' ou 'todos'), iteramos sobre a lista correspondente
    if alvo in GRUPOS:
        lista_execucao = GRUPOS[alvo]
        print(f"⚡ Disparando migração em lote: {alvo.upper()}...")
        
        for etapa in lista_execucao:
            despachar_tarefa(etapa, eng_novo, eng_legado)
            
        print(f"\n🏆 LOTE '{alvo.upper()}' FINALIZADO COM 100% DE INTEGRIDADE!")
        
    # Se o alvo for uma tarefa específica, executamos de forma isolada
    else:
        print(f"🐛 Modo Debug Ativado: Execução isolada.")
        despachar_tarefa(alvo, eng_novo, eng_legado)


if __name__ == "__main__":
    main()