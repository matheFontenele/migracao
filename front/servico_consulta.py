"""Consultas exclusivamente read-only ao legado para a tela de consulta.

Nada aqui escreve no banco do legado: apenas SELECTs. A tela serve para o
usuário enxergar tombos repetidos e o histórico completo de cada cópia, e
tratar os dados diretamente no banco do sistema antigo.
"""
from sqlalchemy import text

# Movimentos que deixam a cópia em posse de um cliente e que a devolvem.
TIPOS_ALUGUEL = (1, 5)
TIPOS_DEVOLUCAO = (2, 3, 4, 6, 8)
TIPOS_RELEVANTES = (1, 2, 3, 4, 5, 6, 8)

LIMITE_MOVIMENTOS = 200
LIMITE_COPIAS = 100
LIMITE_ITENS = 200

# Rótulos usados pelo sistema antigo quando diferem da tabela `status`.
# O id 14 não existe mais em `status`, mas ainda é usado pelos equipamentos.
ROTULOS = {1: "LIBERADO P/ LOCACAO", 14: "SEM CONSERTO"}

# Chips de situação exatamente como na tela antiga (ordem e cores).
CHIPS = [
    ("alugados", "Alugados", 2, "verde"),
    ("aguardando-devolucao", "Aguardando Devolução", 8, "laranja"),
    ("reservado", "Reservado", 3, "azul"),
    ("uso-interno", "Uso Interno", 5, "roxo"),
    ("emprestado", "Emprestado", 4, "amarelo"),
    ("manutencao", "Manutenção", 6, "cinza"),
    ("sem-conserto", "Sem Conserto", 14, "cinza-escuro"),
    ("garantia", "Garantia", 7, "agua"),
    ("liberado", "Liberado Para Locação", 1, "branco"),
    ("baixado", "Baixado", 9, "rosa"),
]


def _texto(valor):
    if valor is None:
        return ""
    return str(valor).strip()


def _data(valor, formato="%d/%m/%Y"):
    if not hasattr(valor, "strftime"):
        return ""
    if getattr(valor, "year", 0) and valor.year < 1900:
        return ""
    return valor.strftime(formato)


def _data_hora(valor):
    return _data(valor, "%d/%m/%Y %H:%M")


def _situacao_nome(situacao_id, nome=None):
    if situacao_id in ROTULOS:
        return ROTULOS[situacao_id]
    return _texto(nome)


def _cliente(cliente_id, nome):
    nome = _texto(nome)
    if cliente_id and nome:
        return f"{int(cliente_id)} - {nome}"
    if cliente_id:
        return str(int(cliente_id))
    return ""


def _ids_sql(ids):
    marcas = ", ".join(f":i{n}" for n in range(len(ids)))
    return marcas, {f"i{n}": i for n, i in enumerate(ids)}


def _derivados(conn, ids):
    """Último movimento relevante de cada cópia.

    `situacao_id` do legado fica defasado; para saber se a cópia está alugada
    e para quem usamos o movimento mais recente de aluguel/devolução.
    """
    ids = [i for i in ids if i]
    if not ids:
        return {}
    marcas, params = _ids_sql(ids)
    linhas = conn.execute(text(f"""
        SELECT x.equipamento_id, x.movimento_id, x.tipo_id, x.data,
               x.cliente_id, cl.nome_razao_social
        FROM (
            SELECT mi.equipamento_id, m.id movimento_id, m.tipo_id, m.data,
                   m.cliente_id,
                   ROW_NUMBER() OVER (PARTITION BY mi.equipamento_id
                                      ORDER BY m.data DESC, m.id DESC) rn
            FROM aluguel_movimento_itens mi
            JOIN aluguel_movimento m ON m.id = mi.movimento_id
            WHERE mi.equipamento_id IN ({marcas})
              AND m.tipo_id IN (1,2,3,4,5,6,8)
        ) x
        LEFT JOIN aluguel_clientes cl ON cl.id = x.cliente_id
        WHERE x.rn = 1
    """), params).mappings().all()
    resultado = {}
    for linha in linhas:
        alugado = linha["tipo_id"] in TIPOS_ALUGUEL
        resultado[int(linha["equipamento_id"])] = {
            "alugado": alugado,
            "movimento_id": linha["movimento_id"],
            "tipo_id": linha["tipo_id"],
            "data": _data_hora(linha["data"]),
            "cliente": _cliente(linha["cliente_id"], linha["nome_razao_social"]) if alugado else "",
            "cliente_desde": _data_hora(linha["data"]) if alugado else "",
        }
    return resultado


def _ultimo_equipamento(conn):
    return int(conn.execute(text(
        "SELECT MAX(numero) FROM aluguel_equipamentos WHERE deleted_at IS NULL"
    )).scalar() or 0)


def filtros(engine):
    with engine.connect() as conn:
        situacoes = conn.execute(text("SELECT id, name FROM status ORDER BY id")).mappings().all()
        contagem = {int(r["situacao_id"]): int(r["qtd"]) for r in conn.execute(text(
            "SELECT situacao_id, COUNT(*) qtd FROM aluguel_equipamentos "
            "WHERE deleted_at IS NULL GROUP BY situacao_id"
        )).mappings()}
        orgaos = conn.execute(text("SELECT ORG_ID id, ORG_NOME nome FROM ORGAOS ORDER BY ORG_NOME")).mappings().all()
        tipos = conn.execute(text("""
            SELECT DISTINCT t.id, t.nome
            FROM aluguel_equipamentos e
            JOIN aluguel_tipos t ON t.id = e.tipo_id
            WHERE e.deleted_at IS NULL
            ORDER BY t.nome
        """)).mappings().all()

        nomes = {int(r["id"]): _texto(r["name"]) for r in situacoes}
        for sid in contagem:
            nomes.setdefault(sid, "")
        lista_situacoes = [
            {"id": sid, "nome": _situacao_nome(sid, nomes.get(sid)), "qtd": contagem.get(sid, 0)}
            for sid in sorted(nomes, key=lambda x: _situacao_nome(x, nomes.get(x)))
        ]
        return {
            "ultimo_equipamento": _ultimo_equipamento(conn),
            "orgaos": [{"id": int(r["id"]), "nome": _texto(r["nome"])} for r in orgaos],
            "tipos": [{"id": int(r["id"]), "nome": _texto(r["nome"])} for r in tipos],
            "situacoes": lista_situacoes,
            "chips": [
                {"chave": chave, "rotulo": rotulo, "situacao_id": sid, "tom": tom,
                 "qtd": contagem.get(sid, 0)}
                for chave, rotulo, sid, tom in CHIPS
            ],
        }


def _condicoes_filtro(numero=None, orgao_id=None, tipo_id=None,
                      situacao_id=None, descricao=None):
    condicoes = ["e.deleted_at IS NULL"]
    params = {}
    if numero:
        condicoes.append("e.numero = :numero")
        params["numero"] = int(numero)
    if orgao_id:
        condicoes.append("e.orgao_id = :orgao_id")
        params["orgao_id"] = int(orgao_id)
    if tipo_id:
        condicoes.append("e.tipo_id = :tipo_id")
        params["tipo_id"] = int(tipo_id)
    if situacao_id:
        condicoes.append("e.situacao_id = :situacao_id")
        params["situacao_id"] = int(situacao_id)
    if descricao:
        condicoes.append("(e.descricao LIKE :descricao OR e.nome LIKE :descricao)")
        params["descricao"] = f"%{descricao}%"
    return " AND ".join(condicoes), params


def _cards_tombo(conn, numeros):
    """Agrega todas as cópias dos tombos pedidos para os cards da listagem."""
    marcas, params = _ids_sql(numeros)
    linhas = conn.execute(text(f"""
        SELECT e.id, e.numero, e.nome, e.descricao, e.situacao_id, e.deleted_at,
               s.name situacao_nome, o.ORG_NOME orgao, t.nome tipo_nome
        FROM aluguel_equipamentos e
        LEFT JOIN status s ON s.id = e.situacao_id
        LEFT JOIN ORGAOS o ON o.ORG_ID = e.orgao_id
        LEFT JOIN aluguel_tipos t ON t.id = e.tipo_id
        WHERE e.numero IN ({marcas})
        ORDER BY e.numero, e.id
    """), params).mappings().all()
    ativos = [int(r["id"]) for r in linhas if r["deleted_at"] is None]
    derivados = _derivados(conn, ativos)

    cards = {
        int(n): {
            "numero": int(n), "copias": 0, "ativas": 0, "excluidas": 0,
            "duplicado": False, "alugados": 0, "descricao": "", "nome": "",
            "orgaos": [], "tipos": [], "situacoes": [], "_sits": {},
        }
        for n in numeros
    }
    for r in linhas:
        card = cards[int(r["numero"])]
        card["copias"] += 1
        descricao = _texto(r["descricao"])
        if not card["descricao"] and descricao:
            card["descricao"] = descricao
        if not card["nome"] and _texto(r["nome"]):
            card["nome"] = _texto(r["nome"])
        if r["deleted_at"] is not None:
            card["excluidas"] += 1
            continue

        card["ativas"] += 1
        sid = int(r["situacao_id"]) if r["situacao_id"] is not None else None
        info = card["_sits"].setdefault(sid if sid is not None else 0, {
            "situacao_id": sid,
            "situacao": _situacao_nome(sid, r["situacao_nome"]),
            "qtd": 0,
        })
        info["qtd"] += 1
        orgao = _texto(r["orgao"])
        if orgao and orgao not in card["orgaos"]:
            card["orgaos"].append(orgao)
        tipo = _texto(r["tipo_nome"])
        if tipo and tipo not in card["tipos"]:
            card["tipos"].append(tipo)
        if (derivados.get(int(r["id"])) or {}).get("alugado"):
            card["alugados"] += 1

    resultado = []
    for n in numeros:
        card = cards[int(n)]
        card["situacoes"] = sorted(
            card.pop("_sits").values(), key=lambda x: (-x["qtd"], x["situacao"])
        )
        card["duplicado"] = card["copias"] > 1
        resultado.append(card)
    return resultado


def tombos(engine, numero=None, orgao_id=None, tipo_id=None, situacao_id=None,
           descricao=None, somente_duplicados=False, page=1, per_page=24):
    """Lista paginada de tombos (`numero`), um card por tombo.

    Os filtros decidem *quais* tombos aparecem; os números do card contam
    todas as cópias do tombo, inclusive as excluídas do legado.
    """
    page = max(1, int(page or 1))
    per_page = min(100, max(1, int(per_page or 24)))
    where, params = _condicoes_filtro(numero, orgao_id, tipo_id, situacao_id,
                                      descricao)
    having = " HAVING COUNT(*) > 1" if somente_duplicados else ""
    with engine.connect() as conn:
        total = int(conn.execute(text(
            "SELECT COUNT(*) FROM (SELECT e.numero FROM aluguel_equipamentos e "
            f"WHERE {where} GROUP BY e.numero{having}) x"
        ), params).scalar() or 0)
        paginas = (total + per_page - 1) // per_page
        if page > max(1, paginas):
            page = max(1, paginas)
        numeros = [int(r["numero"]) for r in conn.execute(text(f"""
            SELECT e.numero FROM aluguel_equipamentos e
            WHERE {where}
            GROUP BY e.numero{having}
            ORDER BY e.numero
            LIMIT :limite OFFSET :deslocamento
        """), {**params, "limite": per_page,
               "deslocamento": (page - 1) * per_page}).mappings()]
        itens = _cards_tombo(conn, numeros) if numeros else []
        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "paginas": paginas,
            "ultimo_equipamento": _ultimo_equipamento(conn),
            "filtros": {
                "numero": numero, "orgao_id": orgao_id, "tipo_id": tipo_id,
                "situacao_id": situacao_id, "descricao": descricao,
                "somente_duplicados": bool(somente_duplicados),
            },
            "itens": itens,
        }


def tombo(engine, numero):
    """Todas as cópias de um tombo, com o estado atual de cada uma."""
    numero = int(numero)
    with engine.connect() as conn:
        linhas = conn.execute(text("""
            SELECT e.id, e.numero, e.numero_antigo, e.nome, e.descricao, e.acessorio,
                   e.uso_interno, e.situacao_id, e.orgao_id, e.tipo_id,
                   e.data_locacao, e.data_garantia, e.data_aquisicao, e.deleted_at,
                   s.name situacao_nome, o.ORG_NOME orgao, t.nome tipo_nome
            FROM aluguel_equipamentos e
            LEFT JOIN status s ON s.id = e.situacao_id
            LEFT JOIN ORGAOS o ON o.ORG_ID = e.orgao_id
            LEFT JOIN aluguel_tipos t ON t.id = e.tipo_id
            WHERE e.numero = :numero
            ORDER BY e.id
        """), {"numero": numero}).mappings().all()
        if not linhas:
            return None

        ids = [int(r["id"]) for r in linhas]
        derivados = _derivados(conn, ids)
        marcas, params = _ids_sql(ids)
        movimentos = {int(r["equipamento_id"]): int(r["qtd"]) for r in conn.execute(text(f"""
            SELECT equipamento_id, COUNT(*) qtd
            FROM aluguel_movimento_itens
            WHERE equipamento_id IN ({marcas})
            GROUP BY equipamento_id
        """), params).mappings()}

        itens, situacoes, orgaos = [], {}, []
        for r in linhas:
            eid = int(r["id"])
            ativo = r["deleted_at"] is None
            sid = int(r["situacao_id"]) if r["situacao_id"] is not None else None
            derivado = derivados.get(eid) or {}
            alugado = ativo and bool(derivado.get("alugado"))
            orgao = _texto(r["orgao"])
            if ativo:
                info = situacoes.setdefault(sid if sid is not None else 0, {
                    "situacao_id": sid,
                    "situacao": _situacao_nome(sid, r["situacao_nome"]),
                    "qtd": 0,
                })
                info["qtd"] += 1
                if orgao and orgao not in orgaos:
                    orgaos.append(orgao)
            itens.append({
                "equipment_id": eid,
                "numero": numero,
                "numero_antigo": int(r["numero_antigo"]) if r["numero_antigo"] else None,
                "nome": _texto(r["nome"]),
                "descricao": _texto(r["descricao"]),
                "observacoes": _texto(r["acessorio"]),
                "tipo_id": int(r["tipo_id"]) if r["tipo_id"] else None,
                "tipo": _texto(r["tipo_nome"]),
                "orgao_id": int(r["orgao_id"]) if r["orgao_id"] else None,
                "orgao": orgao,
                "uso_interno": bool(r["uso_interno"]),
                "situacao_id": sid,
                "situacao": _situacao_nome(sid, r["situacao_nome"]),
                "alugado": alugado,
                "cliente": derivado.get("cliente", "") if alugado else "",
                "cliente_desde": derivado.get("cliente_desde", "") if alugado else "",
                "movimento_id": derivado.get("movimento_id") if ativo else None,
                "movimentos_total": movimentos.get(eid, 0),
                "data_locacao": _data_hora(r["data_locacao"]),
                "data_garantia": _data(r["data_garantia"]),
                "data_aquisicao": _data(r["data_aquisicao"]),
                "deleted_at": _data_hora(r["deleted_at"]),
                "ativo": ativo,
                "situacao_divergente": ativo and alugado != (sid == 2),
            })

        return {
            "numero": numero,
            "copias_total": len(linhas),
            "ativas": sum(1 for r in linhas if r["deleted_at"] is None),
            "excluidas": sum(1 for r in linhas if r["deleted_at"] is not None),
            "situacoes": sorted(situacoes.values(),
                                key=lambda x: (-x["qtd"], x["situacao"])),
            "orgaos": orgaos,
            "itens": itens,
        }


def _movimentos(conn, legado_id):
    linhas = conn.execute(text(f"""
        SELECT m.id, m.numero, m.tipo, m.tipo_id, m.data, m.cliente_id,
               m.orgao_id, m.deleted_at, m.observacoes, m.declaracao_recebimento,
               t.nome tipo_nome, cl.nome_razao_social cliente, o.ORG_NOME orgao
        FROM aluguel_movimento_itens mi
        JOIN aluguel_movimento m ON m.id = mi.movimento_id
        LEFT JOIN aluguel_tipos_movimento t ON t.id = m.tipo_id
        LEFT JOIN aluguel_clientes cl ON cl.id = m.cliente_id
        LEFT JOIN ORGAOS o ON o.ORG_ID = m.orgao_id
        WHERE mi.equipamento_id = :id
        ORDER BY m.data DESC, m.id DESC
        LIMIT {LIMITE_MOVIMENTOS}
    """), {"id": legado_id}).mappings().all()
    if not linhas:
        return []
    ids = [int(r["id"]) for r in linhas]
    marcas, params = _ids_sql(ids)
    pares = {}
    for r in conn.execute(text(f"""
        SELECT substituicao_aluguel_id a, substituicao_devolucao_id d
        FROM aluguel_substituicao
        WHERE substituicao_aluguel_id IN ({marcas})
           OR substituicao_devolucao_id IN ({marcas})
    """), params).mappings():
        pares[int(r["a"])] = int(r["d"])
        pares[int(r["d"])] = int(r["a"])
    par_ids = sorted(set(pares.values()))
    tipos_par = {}
    if par_ids:
        marcas_par, params_par = _ids_sql(par_ids)
        for r in conn.execute(text(f"""
            SELECT id, tipo FROM aluguel_movimento WHERE id IN ({marcas_par})
        """), params_par).mappings():
            tipos_par[int(r["id"])] = _texto(r["tipo"])

    resultado = []
    for r in linhas:
        mid = int(r["id"])
        par_id = pares.get(mid)
        rotulo = f"{tipos_par.get(par_id, '')} - {par_id}" if par_id else "-"
        resultado.append({
            "movimento_id": mid,
            "numero": int(r["numero"]) if r["numero"] is not None else mid,
            "tipo": _texto(r["tipo"]),
            "tipo_id": int(r["tipo_id"]) if r["tipo_id"] else None,
            "tipo_nome": _texto(r["tipo_nome"]),
            "data": _data_hora(r["data"]),
            "cliente_id": int(r["cliente_id"]) if r["cliente_id"] else None,
            "cliente": _cliente(r["cliente_id"], r["cliente"]),
            "orgao": _texto(r["orgao"]),
            "status": "Ativo" if r["deleted_at"] is None else "Excluído",
            "ativo": r["deleted_at"] is None,
            "movimento": rotulo,
            "par_id": par_id,
            "tem_detalhe": (int(r["tipo_id"]) if r["tipo_id"] else 0) in (2, 3),
            "observacoes": _texto(r["observacoes"]),
            "declaracao": _texto(r["declaracao_recebimento"]),
        })
    return resultado


def _copias(conn, numero, atual):
    linhas = conn.execute(text(f"""
        SELECT e.id, e.numero, e.nome, e.situacao_id, e.orgao_id, e.deleted_at,
               s.name situacao_nome, o.ORG_NOME orgao
        FROM aluguel_equipamentos e
        LEFT JOIN status s ON s.id = e.situacao_id
        LEFT JOIN ORGAOS o ON o.ORG_ID = e.orgao_id
        WHERE e.numero = :numero
        ORDER BY e.id
        LIMIT {LIMITE_COPIAS}
    """), {"numero": numero}).mappings().all()
    ids = [int(r["id"]) for r in linhas]
    derivados = _derivados(conn, ids)
    resultado = []
    for r in linhas:
        eid = int(r["id"])
        derivado = derivados.get(eid) or {}
        sid = int(r["situacao_id"]) if r["situacao_id"] is not None else None
        resultado.append({
            "equipment_id": eid,
            "numero": int(r["numero"]),
            "nome": _texto(r["nome"]),
            "situacao_id": sid,
            "situacao": _situacao_nome(sid, r["situacao_nome"]),
            "orgao": _texto(r["orgao"]),
            "alugado": bool(derivado.get("alugado")),
            "cliente": derivado.get("cliente", ""),
            "deleted_at": _data_hora(r["deleted_at"]),
            "atual": eid == int(atual),
        })
    return resultado


def _atrelados(conn, legado_id):
    linhas = conn.execute(text("""
        SELECT DISTINCT e2.id, e2.numero, e2.nome, e2.situacao_id, e2.deleted_at,
               o2.ORG_NOME orgao, s.name situacao_nome,
               ma.data, ma.cliente_id, cl.nome_razao_social cliente, sb.observacao
        FROM aluguel_substituicao sb
        JOIN aluguel_movimento ma ON ma.id = sb.substituicao_aluguel_id
        JOIN aluguel_movimento_itens mi ON mi.movimento_id = ma.id AND mi.equipamento_id = :id
        JOIN aluguel_movimento_itens mdi ON mdi.movimento_id = sb.substituicao_devolucao_id
        JOIN aluguel_equipamentos e2 ON e2.id = mdi.equipamento_id
        LEFT JOIN status s ON s.id = e2.situacao_id
        LEFT JOIN ORGAOS o2 ON o2.ORG_ID = e2.orgao_id
        LEFT JOIN aluguel_clientes cl ON cl.id = ma.cliente_id
        WHERE e2.id <> :id
        ORDER BY ma.data DESC
        LIMIT 50
    """), {"id": legado_id}).mappings().all()
    return [{
        "equipment_id": int(r["id"]),
        "numero": int(r["numero"]) if r["numero"] is not None else None,
        "nome": _texto(r["nome"]),
        "situacao_id": int(r["situacao_id"]) if r["situacao_id"] is not None else None,
        "situacao": _situacao_nome(r["situacao_id"], r["situacao_nome"]),
        "orgao": _texto(r["orgao"]),
        "data": _data_hora(r["data"]),
        "cliente": _cliente(r["cliente_id"], r["cliente"]),
        "observacao": _texto(r["observacao"]),
        "deleted_at": _data_hora(r["deleted_at"]),
    } for r in linhas]


def detalhe(engine, legado_id):
    with engine.connect() as conn:
        base = conn.execute(text("""
            SELECT e.id, e.numero, e.numero_antigo, e.nome, e.descricao, e.acessorio,
                   e.uso_interno, e.situacao_id, e.orgao_id, e.tipo_id,
                   e.data_locacao, e.data_garantia, e.data_aquisicao, e.deleted_at,
                   s.name situacao_nome, o.ORG_NOME orgao, t.nome tipo_nome
            FROM aluguel_equipamentos e
            LEFT JOIN status s ON s.id = e.situacao_id
            LEFT JOIN ORGAOS o ON o.ORG_ID = e.orgao_id
            LEFT JOIN aluguel_tipos t ON t.id = e.tipo_id
            WHERE e.id = :id
        """), {"id": legado_id}).mappings().first()
        if not base:
            return None
        derivado = _derivados(conn, [legado_id]).get(legado_id) or {}
        movimentos = _movimentos(conn, legado_id)
        movimentos_total = int(conn.execute(text(
            "SELECT COUNT(*) FROM aluguel_movimento_itens WHERE equipamento_id = :id"
        ), {"id": legado_id}).scalar() or 0)
        copias = _copias(conn, int(base["numero"]), legado_id)
        atrelados = _atrelados(conn, legado_id)
        sid = int(base["situacao_id"]) if base["situacao_id"] is not None else None
        return {
            "equipamento": {
                "equipment_id": int(base["id"]),
                "numero": int(base["numero"]),
                "numero_antigo": int(base["numero_antigo"]) if base["numero_antigo"] else None,
                "nome": _texto(base["nome"]),
                "descricao": _texto(base["descricao"]),
                "observacoes": _texto(base["acessorio"]),
                "uso_interno": bool(base["uso_interno"]),
                "situacao_id": sid,
                "situacao": _situacao_nome(sid, base["situacao_nome"]),
                "orgao_id": int(base["orgao_id"]) if base["orgao_id"] else None,
                "orgao": _texto(base["orgao"]),
                "tipo_id": int(base["tipo_id"]) if base["tipo_id"] else None,
                "tipo": _texto(base["tipo_nome"]),
                "data_locacao": _data_hora(base["data_locacao"]),
                "data_garantia": _data(base["data_garantia"]),
                "data_aquisicao": _data(base["data_aquisicao"]),
                "deleted_at": _data_hora(base["deleted_at"]),
                "ativo": base["deleted_at"] is None,
                "situacao_divergente": (
                    base["deleted_at"] is None
                    and bool(derivado.get("alugado")) != (sid == 2)
                ),
            },
            "alugado": bool(derivado.get("alugado")),
            "cliente_atual": derivado.get("cliente", ""),
            "cliente_desde": derivado.get("cliente_desde", ""),
            "ultima_movimentacao": derivado.get("movimento_id"),
            "copias_total": len(copias),
            "copias": copias,
            "movimentos_total": movimentos_total,
            "movimentos": movimentos,
            "atrelados": atrelados,
        }


def movimento(engine, movimento_id):
    with engine.connect() as conn:
        base = conn.execute(text("""
            SELECT m.id, m.numero, m.tipo, m.tipo_id, m.data, m.cliente_id, m.orgao_id,
                   m.deleted_at, m.observacoes, m.declaracao_recebimento,
                   t.nome tipo_nome, cl.nome_razao_social cliente, o.ORG_NOME orgao
            FROM aluguel_movimento m
            LEFT JOIN aluguel_tipos_movimento t ON t.id = m.tipo_id
            LEFT JOIN aluguel_clientes cl ON cl.id = m.cliente_id
            LEFT JOIN ORGAOS o ON o.ORG_ID = m.orgao_id
            WHERE m.id = :id
        """), {"id": movimento_id}).mappings().first()
        if not base:
            return None
        pares = conn.execute(text("""
            SELECT substituicao_aluguel_id a, substituicao_devolucao_id d
            FROM aluguel_substituicao
            WHERE substituicao_aluguel_id = :id OR substituicao_devolucao_id = :id
        """), {"id": movimento_id}).mappings().first()
        par_id = None
        if pares:
            par_id = int(pares["d"]) if int(pares["a"]) == movimento_id else int(pares["a"])
        par = None
        if par_id:
            outra = conn.execute(text("""
                SELECT m.id, m.tipo, m.numero, m.data, m.cliente_id,
                       cl.nome_razao_social cliente
                FROM aluguel_movimento m
                LEFT JOIN aluguel_clientes cl ON cl.id = m.cliente_id
                WHERE m.id = :id
            """), {"id": par_id}).mappings().first()
            if outra:
                par = {
                    "movimento_id": int(outra["id"]),
                    "tipo": _texto(outra["tipo"]),
                    "numero": int(outra["numero"]) if outra["numero"] is not None else None,
                    "data": _data_hora(outra["data"]),
                    "cliente": _cliente(outra["cliente_id"], outra["cliente"]),
                }
        itens = conn.execute(text(f"""
            SELECT mi.equipamento_id, e.numero, e.nome, mi.antigo_orgao_id, mi.novo_orgao_id,
                   oa.ORG_NOME orgao_anterior, onv.ORG_NOME orgao_novo
            FROM aluguel_movimento_itens mi
            LEFT JOIN aluguel_equipamentos e ON e.id = mi.equipamento_id
            LEFT JOIN ORGAOS oa ON oa.ORG_ID = mi.antigo_orgao_id
            LEFT JOIN ORGAOS onv ON onv.ORG_ID = mi.novo_orgao_id
            WHERE mi.movimento_id = :id
            ORDER BY mi.id
            LIMIT {LIMITE_ITENS}
        """), {"id": movimento_id}).mappings().all()
        return {
            "movimento": {
                "movimento_id": int(base["id"]),
                "numero": int(base["numero"]) if base["numero"] is not None else int(base["id"]),
                "tipo": _texto(base["tipo"]),
                "tipo_id": int(base["tipo_id"]) if base["tipo_id"] else None,
                "tipo_nome": _texto(base["tipo_nome"]),
                "data": _data_hora(base["data"]),
                "cliente": _cliente(base["cliente_id"], base["cliente"]),
                "orgao": _texto(base["orgao"]),
                "status": "Ativo" if base["deleted_at"] is None else "Excluído",
                "observacoes": _texto(base["observacoes"]),
                "declaracao": _texto(base["declaracao_recebimento"]),
                "par": par,
            },
            "itens": [{
                "equipment_id": int(r["equipamento_id"]),
                "numero": int(r["numero"]) if r["numero"] is not None else None,
                "nome": _texto(r["nome"]),
                "orgao_anterior": _texto(r["orgao_anterior"]),
                "orgao_novo": _texto(r["orgao_novo"]),
            } for r in itens],
        }
