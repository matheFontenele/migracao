import argparse
import subprocess
import sys
import os

# Mapeamento das tarefas
TAREFAS = {
    "clientes": {"script": "migracao_cliente.py", "pasta": "clientes"},
    "contratos": {"script": "migracao_contratos.py", "pasta": "contratos"},
    "equipamentos": {"script": "migracao_equipamentos.py", "pasta": "equipamentos"}
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
    
    # Argumento posicional (sem os traços --). O padrão é "todos" se nada for digitado.
    parser.add_argument("alvo", nargs="?", default="todos", 
                        help="Qual etapa executar? (ex: migracao_cliente.py, contratos, ou todos)")
    
    args = parser.parse_args()
    alvo = args.alvo.lower().strip()

    # Normaliza se o usuário digitar "testes" no plural
    if alvo == "testes":
        alvo = "teste"

    # 1. Se o usuário quiser rodar tudo (Apenas scripts de PRODUÇÃO)
    if alvo in ["todos", "todas"]:
        for t in TAREFAS.keys():
            if t == "teste": 
                continue  # 🛡️ Proteção: impede o script de teste de rodar na migração oficial
            rodar_script(t)
        return

    # 2. Se o usuário usou a chave direta (ex: "clientes", "contratos", "teste")
    if alvo in TAREFAS:
        rodar_script(alvo)
        return

    # 3. Se o usuário digitou o nome do script (ex: "teste_migracao_equipamentos.py")
    for chave, config in TAREFAS.items():
        # Aceita o nome exato ou uma variação comum com 's' no plural para evitar erros de digitação
        if config["script"] == alvo or config["script"].replace(".py", "s.py") == alvo:
            rodar_script(chave)
            return

    # 4. Se não encontrou nada
    print(f"❌ Erro: Comando '{alvo}' não reconhecido.")
    print("Tente algo como: python executar_migracao.py teste")
    sys.exit(1)

if __name__ == "__main__":
    main()