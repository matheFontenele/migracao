def _unificar_ministerio_relacoes(self, df: pd.DataFrame) -> pd.DataFrame:
        df_modificado = df.copy()
        
        # Desempacota o dicionário: (Alvo, Origem)
        pref_alvo, pref_origem = MAPA_MINISTERIO_RELACOES["PREFEITURA"]  # (375, 322)
        sec_alvo, sec_origem = MAPA_MINISTERIO_RELACOES["SECRETARIA"]    # (1424, 1347)
        
        # Converte para numérico temporariamente para garantir o match perfeito
        # Isso evita o bug clássico do Pandas achar que '322.0' é diferente de 322
        id_pref_serie = pd.to_numeric(df_modificado['ID_PREFEITURA'], errors='coerce')
        id_sec_serie = pd.to_numeric(df_modificado['ID_SECRETARIA'], errors='coerce')
        
        # ====================================================================
        # 1️⃣ UNIFICAÇÃO DA PREFEITURA
        # ====================================================================
        mask_pref = id_pref_serie == pref_origem  # Busca quem é 322
        
        if mask_pref.any():
            # Tenta pegar o nome real do alvo (375) na própria planilha para manter o padrão
            nomes_pref_alvo = df_modificado.loc[id_pref_serie == pref_alvo, 'PREFEITURA']
            nome_pref_padrao = nomes_pref_alvo.iloc[0] if not nomes_pref_alvo.empty else "MINISTÉRIO DAS RELAÇÕES EXTERIORES"
            
            # Aplica a alteração em todas as linhas afetadas de uma só vez!
            df_modificado.loc[mask_pref, 'ID_PREFEITURA'] = pref_alvo
            df_modificado.loc[mask_pref, 'PREFEITURA'] = nome_pref_padrao
            
        # ====================================================================
        # 2️⃣ UNIFICAÇÃO DA SECRETARIA
        # ====================================================================
        mask_sec = id_sec_serie == sec_origem  # Busca quem é 1347
        
        if mask_sec.any():
            # Tenta pegar o nome real do alvo (1424) na própria planilha
            nomes_sec_alvo = df_modificado.loc[id_sec_serie == sec_alvo, 'SECRETARIA']
            nome_sec_padrao = nomes_sec_alvo.iloc[0] if not nomes_sec_alvo.empty else "SECRETARIA MRE"
            
            # Aplica a alteração em todas as linhas afetadas de uma só vez!
            df_modificado.loc[mask_sec, 'ID_SECRETARIA'] = sec_alvo
            df_modificado.loc[mask_sec, 'SECRETARIA'] = nome_sec_padrao
            
        return df_modificado