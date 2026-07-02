def _get_hierarchical_customer(self, nome_planilha):
        nome_alvo = ultra_normalizar(nome_planilha)
        if not nome_alvo: return None

        if nome_alvo in self.customer_cache:
            d = self.customer_cache[nome_alvo]
            print(f"   ✅ Match (Nome Exato): '{nome_planilha}' -> '{d['debug']}'")
            return d  # 👈 Agora retorna o dicionário inteiro, e não apenas o número!

        match_tokens = self._match_por_tokens(nome_alvo)
        if match_tokens:
            print(f"   ✅ Match (Fuzzy Seguro): '{nome_planilha}' -> '{match_tokens['debug']}'")
            return match_tokens  # 👈 Retorna o dicionário inteiro

        return None