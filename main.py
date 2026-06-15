import argparse
import subprocess
import sys
import os

# Mapeamento das tarefas
TAREFAS = {
    "clientes": {"script": "migracao_cliente.py", "pasta": "clientes"},
    "contratos": {"script": "migracao_contratos.py", "pasta": "contratos"},
    "contratantes": {"script": "migracao_contratantes.py", "pasta": "contratos"},
    "equipamentos": {"script": "migracao_equipamentos.py", "pasta": "equipamentos"},
    "movimentos_aluguel": {"script": "migracao_movimentos_aluguel.py", "pasta": "movimentos"}
}

def rodar_script(nome_tarefa):
    config = TAREFAS[nome_tarefa]
    script_path = os.path.join(config["pasta"], config["script"])
    
    print(f"\n🚀 EXECUTANDO: {nome_tarefa.upper()}...")
    print(f"📄 Arquivo: {script_path}")
    
    # Executa o script. O cwd garante que o script enxergue os arquivos locais dele
    processo = subprocess.run([sys.executable, config["script"]], cwd=config["pasta"])
    
    if processo.returncode == 0:
        print(f"✅ {nome_tarefa.upper()} concluído com sucesso.")
    else:
        print(f"❌ {nome_tarefa.upper()} FALHOU com código {processo.returncode}.")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Pipeline de Migração Individual ou Total")
    
    # Argumento posicional. O padrão é "todos" se nada for digitado.
    parser.add_argument("alvo", nargs="?", default="todos", 
                        help="Qual etapa executar? (clientes, contratos, contratantes, equipamentos, movimentos_aluguel ou todos)")
    
    args = parser.parse_args()
    alvo = args.alvo.lower().strip()
    
    # 1. Se o alvo for "todos", executa a esteira na ordem correta de chaves estrangeiras
    if alvo == "todos":
        print("⚡ Iniciando a execução completa do pipeline de migração...")
        # 🎯 Agora a lista bate perfeitamente com as chaves do dicionário TAREFAS
        ordem_execucao = ["clientes", "contratos", "contratantes", "equipamentos", "movimentos_aluguel"]
        for tarefa in ordem_execucao:
            rodar_script(tarefa)
        print("\n🏆 PIPELINE EXECUTADO COM SUCESSO TOTAL!")
        
    # 2. Se for uma tarefa individual válida (ex: "movimentos_aluguel")
    elif alvo in TAREFAS:
        rodar_script(alvo)
        
    # 3. Comando não reconhecido
    else:
        print(f"❌ Erro: Alvo de migração '{alvo}' não reconhecido.")
        print("\nOpções disponíveis:")
        print("  python main.py todos               (Roda tudo na sequência certa)")
        print("  python main.py clientes            (Apenas Clientes)")
        print("  python main.py contratos           (Apenas Contratos)")
        print("  python main.py contratantes        (Apenas Vínculo de Contratantes)")
        print("  python main.py equipamentos        (Apenas Equipamentos)")
        print("  python main.py movimentos_aluguel  (Apenas Movimentações de Aluguel)") # 🔥 Atualizado na ajuda
        sys.exit(1)

if __name__ == "__main__":
    main()