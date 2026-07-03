def buscar_equipamentos_novo_por_tombo(self, lista_tombos: list) -> dict:

        if not lista_tombos:
            return {}

        tombos_formatados = [f"'{str(t).strip()}'" for t in lista_tombos]
        lista_tombos_sql = "(" + ", ".join(tombos_formatados) + ")"
        
        # O SEU SQL APLICADO AQUI
        query = f"""
            SELECT 
                eq.id, 
                eq.number, 
                eq.name, 
                eq.last_movement_item_customer_id, 
                eq.deleted_at 
            FROM equipments eq 
            WHERE eq.number IN {lista_tombos_sql} AND eq.deleted_at IS NULL
        """
        
        # Executa a query diretamente no banco NOVO
        df_resultado = pd.read_sql(text(query), self.engine_new)

        # Monta o dicionário de tradução ultra-rápida (O(1))
        dict_res = {}
        for _, row in df_resultado.iterrows():
            tombo_chave = limpar_codigo(row['number'])
            if tombo_chave:
                dict_res[tombo_chave] = int(row['id'])
                
        return dict_res