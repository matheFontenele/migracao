try:
        df_bruto = extrair_dados_legado(engine_legado)
    except Exception as e:
        print(f"❌ Erro crítico na extração de dados: {e}")
        return

    print(f"📊 Total de linhas brutas do legado: {len(df_bruto)}")
    df = limpar_e_tratar_dados(df_bruto)
    
    linhas_antes = len(df)
    df = df[df['ORGANIZACAO'].apply(lambda x: descobrir_id_organizacao_destino(x) not in ORGANIZACOES_BLOQUEADAS)]
    if linhas_antes != len(df):
        print(f"🛑 [FILTRO] Removidas {linhas_antes - len(df)} linhas bloqueadas.")
        
    # 1. Garante uma coluna de ID numérico limpa para aplicar os filtros físicos e interceptadores
    df['id_clean'] = pd.to_numeric(df['ID_CLIENTE'], errors='coerce').fillna(0).astype(int)

    # 🚨 NOVO FILTRO: Remoção cirúrgica de clientes bloqueados manualmente (2131 e 2707)
    linhas_antes_cli = len(df)
    df = df[~df['id_clean'].isin(CLIENTES_BLOQUEADOS)]
    if linhas_antes_cli != len(df):
        print(f"🛑 [FILTRO] Removidos {linhas_antes_cli - len(df)} clientes específicos bloqueados manualmente.")

    # INJEÇÃO DE EXCEÇÕES (Processando dados já limpos e filtrados)
    df = regionalizar_pcpb(df)
    df = unificar_sao_luis(df)

    print("Construindo clientes...")