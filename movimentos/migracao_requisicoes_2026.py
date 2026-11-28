"""Importa as requisições de docs/requisicao2026.csv para o banco refatorado.

Gera `service_orders` + `service_order_items` e, para as linhas separadas,
`movements` + `movement_items`.

Regras de entrada (pedido de 27/11/2026):

- Só entram linhas com `Req` = **Separado** ou **Parcial**; ATENDIDA, Cancelada,
  ENVIADO, Solicitado Envio, Sem estoque e vazio ficam de fora e vão para o
  relatório de não migradas.
- O movimento nasce apenas de linhas **Separado** cujo `PAT` aponta para um
  equipamento existente (o PAT é o equipamento que atende a requisição).
- Em linhas **Parcial**, só entram itens com `PAT` preenchido cujo equipamento
  esteja **disponível para locação** (`equipments.status_id = 1`); os demais
  itens ficam de fora da requisição.
- Uma linha só é **substituição** quando `Substituição ou Novo` tem valor
  numérico e o tombo existe em `equipments.number`; caso contrário é avulsa.
- Linha cujo `Cliente` não é identificado no banco é ignorada.

Este módulo é o ÚLTIMO passo da migração: depende de clientes, usuários,
organizações, equipamentos, tipos e produtos já migrados.

Dry-run é o padrão. Nada é gravado sem `--execute`.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text
from tqdm import tqdm

from utils.clientes_requisicoes import IndiceClientes


BASE_DIR = Path(__file__).resolve().parents[1]
ARQUIVO_FONTE = BASE_DIR / "docs" / "requisicao2026.csv"
ARQUIVO_AUDITORIA = BASE_DIR / "ajustes" / "auditoria_requisicoes_2026.csv"
ARQUIVO_NAO_MIGRADAS = BASE_DIR / "ajustes" / "requisicoes_2026_nao_migradas.csv"
ARQUIVO_MAPA_INSUMOS = BASE_DIR / "ajustes" / "insumos_para_produtos.csv"
DIR_LOGS = BASE_DIR / "ajustes" / "logs_requisicoes_2026"
MARCADOR = "Migração REQUISIÇÃO 2026.csv"

# ---------------------------------------------------------------------------
# De/para da planilha
# ---------------------------------------------------------------------------
USUARIOS = {
    "VICTOR": "victoralves",
    "BEATRIZ": "BEATRIZCARDOSO",
    "DAVI": "DAVISOUZA",
    "SANTIAGO": "SANTIAGO",
    "IMNA": "imna",
    "ATILA": "atilacosta",
}
# `EMANUEL` não existe como usuário no destino; as 46 linhas dele (e as 29 sem
# solicitante) usam o usuário administrativo e ficam registradas na auditoria.
USUARIO_PADRAO = "admin"

ORGANIZACOES = {
    "AS SISTEMAS": 1378,
    "MOREIA": 1122,
    "ALUCOM": 1115,
    # ADI é o departamento/estoque da ALUCOM (orgs legadas 1351 TONER ADI e
    # 1360 PEÇAS ADI). Trocar aqui se a decisão for criar uma organização própria.
    "ADI": 1115,
    "IP": 1311,
}

TRANSPORTES = {"ROTA": 1, "RETIRADA": 2, "TRANSPORTADORA": 3}

# `Separado por` -> username no destino. Nome fora deste mapa (ex.: Bruno,
# Logística, Neto) cai no usuário padrão (admin) em vez de bloquear o
# movimento; o fallback fica registrado na auditoria.
SEPARADORES = {
    "MARCELO": "Marcelo",
    "ROBERTO": "Roberto",
    "CARLOS JR": "carlosbezerra",
    "DANILSON": "DANILSON",
    "ELISANGELA": "elisangela",
}

TIPOS_MOVIMENTO = {"AVULSO": 7, "SUBSTITUICAO": 2}

# Só estas linhas geram requisição/movimento nesta migração.
REQS_MIGRADAS = frozenset({"SEPARADO", "PARCIAL"})
# `equipments.status_id` de "DISPONÍVEL PARA LOCAÇÃO" (tabela `status`).
STATUS_DISPONIVEL_LOCACAO = 1

# Departamento do item em `service_order_departments`:
# - item com patrimônio (PAT/tombo) -> EXPEDIÇÃO
# - item sem patrimônio -> ELISANGELA
# O ALMOXARIFADO não é usado na importação de requisições.
DEPARTAMENTO_EQUIPAMENTO = "EXPEDIÇÃO"
DEPARTAMENTO_PECAS_ADI = "ELISANGELA"

# Palavras que identificam peças/consumíveis e equipamentos. São usadas quando o
# catálogo não resolve o item e também para corrigir o catálogo de insumos, que
# repete descrições de equipamento (notebook, impressora, nobreak...).
PALAVRAS_INSUMO = (
    "TONER", "TINTA", "CARTUCHO", "REVELADOR", "REVELACAO", "REFIL", "RIBBON",
    "CABO", "MEMORIA", "SSD", "HD", "DISCO", "FONTE", "MOUSE", "TECLADO",
    "ADAPTADOR", "CARREGADOR", "PLACA", "COOLER", "BATERIA", "PILHA", "FONE",
    "PENDRIVE", "CHIP", "CILINDRO", "BOBINA", "ETIQUETA", "PAPEL", "FUSOR",
    "PICKUP", "ROLLER", "SENSOR", "CONECTOR", "EXTENSAO", "REGUA", "FILTRO",
    "PROCESSADOR", "GAVETA", "BANDEJA", "TAMPA", "TECLA", "MEMBRANA",
    "PELICULA", "KIT DE TINTA", "UNIDADE DE REVELACAO",
    # Modelos/códigos e peças recorrentes na planilha 2026.
    "DDR", "NVME", "FOTOCONDUTOR", "CABECOTE", "MANTA TERMICA", "ROLLETE",
    "RECIPIENTE DE RESIDUO", "ADESIVO", "MOLA", "SACHE", "DISPLAYPORT", "TK-",
    "TK ",
)
PALAVRAS_EQUIPAMENTO = (
    "MICRO", "COMPUTADOR", "NOTEBOOK", "NETBOOK", "MONITOR", "IMPRESSORA",
    "MULTIFUNCIONAL", "SCANNER", "NOBREAK", "ESTABILIZADOR", "TRANSFORMADOR",
    "PROJETOR", "TELA", "TV", "SERVIDOR", "SWITCH", "ROTEADOR", "THINKCENTRE",
    "MINI PC", "ALL IN ONE", "TABLET", "CELULAR", "TELEFONE", "GELAGUA",
    "BEBEDOURO", "VENTILADOR", "FRAGMENTADORA", "DUPLICADOR", "PLOTTER",
    "FAX", "CAMERA", "WEBCAM", "TOTEM", "MODULO", "RACK",
    "PONTO DE ACESSO", "SCANER", "TINY",
)

GENERICOS = {"DE", "DA", "DO", "DAS", "DOS", "E", "PARA", "P", "EM", "COM"}

RE_SERIAL = re.compile(
    r"\b(NS|N/S|S/N|SN|SERIE|PATRIMONIO)\b.*$", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Normalização e conversões
# ---------------------------------------------------------------------------
def normalizar(valor: object) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", texto).strip().upper()


# Clientes da planilha que não entram nesta migração (pedido de 24/11/2026).
# A comparação usa `normalizar`, então grafias com/sem acento ou caixa
# diferente continuam sendo o mesmo cliente. Não inclui as demais unidades de
# São Luís (BASE SÃO LUÍS, SEMCAS, SEMUSC, SMTT, PGM, SEMIT).
CLIENTES_IGNORADOS_REQUISICOES_2026 = frozenset({
    "SECRETARIA DE SAUDE DE SÃO LUIS",
})


def tokens_relevantes(texto: str, minimo: int = 3) -> set[str]:
    return {t for t in normalizar(texto).split()
            if len(t) >= minimo and t not in GENERICOS}


def data_planilha(valor: object) -> datetime | None:
    texto = str(valor or "").strip()
    if not texto:
        return None
    parsed = pd.to_datetime(texto, dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0)


def numero(valor: object) -> int | None:
    texto = str(valor or "").strip()
    if not texto:
        return None
    try:
        return int(float(texto.replace(",", ".")))
    except ValueError:
        return None


RE_PAT_COPIAS = re.compile(r"^\s*(\d+)\s*(?:[-/]\s*\d+\s*)+$")


def numero_pat(valor: object) -> int | None:
    """Patrimônio de um campo PAT da planilha.

    Às vezes o tombo vem repetido com o sufixo das cópias (`19687-1/3`,
    `19963-1/1`): o patrimônio continua sendo o número da frente. O restante
    do campo é só a indicação de qual cópia do tombo está no item.
    """
    texto = str(valor or "").strip()
    if not texto:
        return None
    direto = numero(texto)
    if direto is not None:
        return direto
    achado = RE_PAT_COPIAS.match(texto)
    return int(achado.group(1)) if achado else None


def numeros(valor: object) -> tuple[int, ...]:
    """Extrai todos os números de um campo (aceita listas tipo `1, 2, 3`)."""
    return tuple(int(n) for n in re.findall(r"\d{2,}", str(valor or "")))


def limpar_serial(texto: object) -> str:
    """Remove número de série/patrimônio para casar o item com o catálogo."""
    base = RE_SERIAL.sub("", str(texto or ""))
    base = re.sub(r"\b\d{5,}\b", " ", base)
    return re.sub(r"\s+", " ", base).strip(" ,-/")


def selecionar_organizacao(etiqueta: object) -> int | None:
    tag = normalizar(etiqueta)
    for chave, org_id in ORGANIZACOES.items():
        if chave in tag:
            return org_id
    return None


def palavra_chave_tipo(texto: str) -> str | None:
    """Classifica por palavra-chave usando a posição do termo no texto.

    A descrição costuma listar a peça antes da especificação ("MEMORIA PARA
    NOTEBOOK"), mas o equipamento vem primeiro quando é ele o pedido
    ("NOTEBOOK I5 8GB SSD 512GB"). Por isso vence a palavra que aparece
    primeiro, e não a ordem fixa das listas.
    """
    alvo = f" {normalizar(texto)} "

    def primeira(palavras: tuple[str, ...]) -> int | None:
        achados = [alvo.find(f" {p}") for p in palavras if f" {p}" in alvo]
        return min(achados) if achados else None

    equipamento = primeira(PALAVRAS_EQUIPAMENTO)
    insumo = primeira(PALAVRAS_INSUMO)
    if equipamento is None and insumo is None:
        return None
    if equipamento is None:
        return "INSUMO"
    if insumo is None:
        return "EQUIPAMENTO"
    return "EQUIPAMENTO" if equipamento <= insumo else "INSUMO"


# ---------------------------------------------------------------------------
# Similaridade
# ---------------------------------------------------------------------------
class IndiceSimilaridade:
    """Busca por similaridade com pré-filtro por tokens.

    Comparar cada nome da planilha contra todo o catálogo (milhares de linhas)
    seria lento, então os candidatos são reduzidos por interseção de tokens.
    """

    def __init__(self, registros: list[dict], campos: tuple[str, ...] = ("name", "alias")):
        self.registros = registros
        self.chaves: list[list[str]] = []
        self.indice: dict[str, set[int]] = defaultdict(set)
        for posicao, registro in enumerate(registros):
            chaves = [normalizar(registro.get(campo)) for campo in campos
                      if registro.get(campo)]
            chaves = [c for c in chaves if c]
            self.chaves.append(chaves)
            for token in {t for chave in chaves for t in tokens_relevantes(chave)}:
                self.indice[token].add(posicao)

    def buscar(self, texto: object, limite: float = 0.80, margem: float = 0.05,
               topo: int = 5) -> tuple[dict | None, float, list[dict]]:
        alvo = normalizar(texto)
        if not alvo:
            return None, 0.0, []
        candidatos: set[int] = set()
        for token in tokens_relevantes(alvo):
            candidatos |= self.indice.get(token, set())
        if len(candidatos) < 2:
            candidatos = set(range(len(self.registros)))
        pontuados: list[tuple[float, int]] = []
        for posicao in candidatos:
            melhor = 0.0
            for chave in self.chaves[posicao]:
                comparador = difflib.SequenceMatcher(None, alvo, chave)
                if comparador.real_quick_ratio() <= melhor:
                    continue
                if comparador.quick_ratio() <= melhor:
                    continue
                melhor = max(melhor, comparador.ratio())
            if melhor:
                pontuados.append((melhor, posicao))
        if not pontuados:
            return None, 0.0, []
        pontuados.sort(reverse=True)
        melhor_score, melhor_posicao = pontuados[0]
        segundo = pontuados[1][0] if len(pontuados) > 1 else 0.0
        sugestoes = [dict(self.registros[p], _similaridade=round(s, 4))
                     for s, p in pontuados[:topo]]
        if melhor_score < limite or melhor_score - segundo < margem:
            return None, melhor_score, sugestoes
        return self.registros[melhor_posicao], melhor_score, sugestoes


@dataclass
class Referencias:
    clientes: IndiceClientes
    produtos: IndiceSimilaridade
    produtos_insumo: IndiceSimilaridade
    tipos: IndiceSimilaridade
    usuarios: dict[str, int]
    organizacoes: set[int]
    transportes: set[int]
    equipamentos: dict[int, list[dict]] = field(default_factory=dict)
    produtos_por_id: dict[int, str] = field(default_factory=dict)
    mapa_insumos: dict[str, int] = field(default_factory=dict)


def _chave_produto(valor: object) -> str:
    """Chave de nome de produto: ignora acento, caixa, espaço e pontuação."""
    return re.sub(r"[^A-Z0-9]", "", normalizar(valor))


def carregar_mapa_insumos(conn,
                          arquivo: Path = ARQUIVO_MAPA_INSUMOS) -> dict[str, int]:
    """De/para curado: descrição da planilha -> produto de insumo existente.

    O catálogo não cobre parte das descrições (toner de modelo específico,
    fonte, SSD). Sem `product_id`, a linha cai na lista de EQUIPAMENTOS da
    requisição — o sistema identifica insumo pela presença do produto. Só
    aceita produto com `is_asset = 0` para não transformar insumo em equipamento.

    A coluna `produto` (nome) manda: os IDs do CSV são apenas uma pista, porque
    o catálogo local é recriado a cada reset e os IDs andam. O nome é resolvido
    contra os insumos ativos; o ID só desempata (e serve de recurso quando a
    linha antiga não tem nome).
    """
    if not arquivo.exists():
        return {}

    catalogo = [dict(linha) for linha in conn.execute(text(
        "SELECT id, name, is_asset FROM products WHERE deleted_at IS NULL"
    )).mappings()]
    insumos_por_id = {
        int(linha["id"]): str(linha.get("name") or "")
        for linha in catalogo if not int(linha["is_asset"])
    }
    insumos_por_nome: dict[str, list[int]] = defaultdict(list)
    for produto_id, nome in insumos_por_id.items():
        chave_nome = _chave_produto(nome)
        if chave_nome:
            insumos_por_nome[chave_nome].append(produto_id)

    mapa: dict[str, int] = {}
    por_nome = por_id = 0
    with arquivo.open(encoding="utf-8-sig", newline="") as arquivo_csv:
        for linha in csv.DictReader(arquivo_csv):
            alias = (linha.get("alias") or "").strip()
            produto_id = numero(linha.get("produto_id"))
            nome_produto = (linha.get("produto") or "").strip()
            if not alias or (not produto_id and not nome_produto):
                continue

            candidatos = insumos_por_nome.get(_chave_produto(nome_produto), [])
            if len(candidatos) == 1:
                escolhido = candidatos[0]
                por_nome += 1
            elif produto_id in candidatos:
                escolhido = produto_id
                por_nome += 1
            elif (candidatos and produto_id in insumos_por_id
                  and normalizar(insumos_por_id[produto_id])
                  == normalizar(nome_produto)):
                escolhido = produto_id
                por_nome += 1
            elif candidatos:
                # Catálogo com o mesmo insumo duplicado: escolha determinística.
                escolhido = min(candidatos)
                print(f"  aviso {arquivo.name}: {alias!r} -> "
                      f"{nome_produto!r} tem candidatos {sorted(candidatos)}; "
                      f"usando {escolhido}")
                por_nome += 1
            elif produto_id in insumos_por_id:
                escolhido = produto_id
                por_id += 1
            else:
                raise RuntimeError(
                    f"{arquivo.name}: não achei insumo ativo para {alias!r} "
                    f"(produto {produto_id}, nome {nome_produto!r})")

            chave = normalizar(alias)
            if chave in mapa and mapa[chave] != escolhido:
                raise RuntimeError(
                    f"{arquivo.name}: descrição repetida com produtos "
                    f"diferentes: {alias!r}")
            mapa[chave] = escolhido

    if mapa:
        print(f"  mapa de insumos: {len(mapa)} alias(es) "
              f"(por nome={por_nome}, por id={por_id})")
    return mapa


def carregar_referencias(conn, conn_legado) -> Referencias:
    produtos = [dict(r) for r in conn.execute(text(
        "SELECT id, name, type_id, is_asset FROM products WHERE deleted_at IS NULL"
    )).mappings()]
    tipos = [dict(r) for r in conn.execute(text(
        "SELECT id, name FROM types WHERE deleted_at IS NULL"
    )).mappings()]
    usuarios = {normalizar(r.username): int(r.id) for r in conn.execute(text(
        "SELECT id, username FROM users WHERE deleted_at IS NULL"
    ))}
    organizacoes = {int(r.id) for r in conn.execute(text(
        "SELECT id FROM organizations WHERE deleted_at IS NULL"
    ))}
    transportes = {int(r.id) for r in conn.execute(text(
        "SELECT id FROM service_order_mode_transports WHERE deleted_at IS NULL"
    ))}
    return Referencias(
        clientes=IndiceClientes.carregar(conn_legado, conn),
        produtos=IndiceSimilaridade(produtos),
        produtos_insumo=IndiceSimilaridade(
            [p for p in produtos if not int(p["is_asset"])]),
        tipos=IndiceSimilaridade(tipos),
        usuarios=usuarios,
        organizacoes=organizacoes,
        transportes=transportes,
        produtos_por_id={int(p["id"]): str(p["name"]) for p in produtos},
        mapa_insumos=carregar_mapa_insumos(conn),
    )


def carregar_equipamentos(conn, numeros_necessarios: set[int]) -> dict[int, list[dict]]:
    equipamentos: dict[int, list[dict]] = defaultdict(list)
    if not numeros_necessarios:
        return equipamentos
    consulta = text("""
        SELECT id, number, status_id, name, current_organization_id
        FROM equipments
        WHERE deleted_at IS NULL
    """)
    for linha in conn.execute(consulta).mappings():
        try:
            numero_equipamento = int(str(linha["number"]).strip())
        except (TypeError, ValueError):
            continue
        if numero_equipamento in numeros_necessarios:
            equipamentos[numero_equipamento].append({
                "id": int(linha["id"]),
                "numero": numero_equipamento,
                "status_id": int(linha["status_id"]),
                "nome": linha["name"],
                "organizacao": linha["current_organization_id"],
            })
    return equipamentos


# ---------------------------------------------------------------------------
# Classificação do item (equipamento x insumo)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClassificacaoItem:
    tipo: str                 # EQUIPAMENTO | INSUMO | INDEFINIDO
    product_id: int | None
    type_id: int | None
    referencia: str
    similaridade: float
    metodo: str


def classificar_item(alias: str, refs: Referencias,
                     cache: dict[str, ClassificacaoItem]) -> ClassificacaoItem:
    chave = normalizar(alias)
    if chave in cache:
        return cache[chave]

    # De/para curado tem precedência: o catálogo não cobre estas descrições e
    # sem `product_id` a linha apareceria na lista de EQUIPAMENTOS.
    produto_mapeado = refs.mapa_insumos.get(chave)
    if produto_mapeado:
        classificacao = ClassificacaoItem(
            tipo="INSUMO", product_id=produto_mapeado, type_id=None,
            referencia=refs.produtos_por_id.get(produto_mapeado, ""),
            similaridade=1.0, metodo="mapa insumos")
        cache[chave] = classificacao
        return classificacao

    base = limpar_serial(alias) or alias
    produto, score, _ = refs.produtos.buscar(base, limite=0.0, margem=0.0)
    tipo_kw = palavra_chave_tipo(base)
    referencia = produto["name"] if produto else ""
    tipo = "INDEFINIDO"
    product_id = type_id = None

    if produto and score >= 0.85:
        tipo = "EQUIPAMENTO" if int(produto["is_asset"]) else "INSUMO"
        type_id = int(produto["type_id"]) if produto.get("type_id") else None
        if tipo == "INSUMO" and tipo_kw == "EQUIPAMENTO":
            # O catálogo de insumos repete descrições de equipamento
            # ("NOTEBOOK I7...", "IMPRESSORA...", "NOBREAK..."). Nesses casos a
            # descrição manda: é equipamento, só não veio com PAT.
            tipo = "EQUIPAMENTO"
            metodo = f"produto {score:.2f}/palavra-chave equipamento"
        else:
            product_id = int(produto["id"]) if tipo == "INSUMO" else None
            metodo = f"produto {score:.2f}"
    elif tipo_kw:
        tipo = tipo_kw
        metodo = "palavra-chave"
    elif produto and score >= 0.70:
        tipo = "EQUIPAMENTO" if int(produto["is_asset"]) else "INSUMO"
        type_id = int(produto["type_id"]) if produto.get("type_id") else None
        product_id = int(produto["id"]) if tipo == "INSUMO" else None
        metodo = f"produto(fraco) {score:.2f}"
    else:
        tipo_registro, score_tipo, _ = refs.tipos.buscar(base, limite=0.85, margem=0.05)
        if tipo_registro:
            tipo = "EQUIPAMENTO"
            type_id = int(tipo_registro["id"])
            referencia = referencia or tipo_registro["name"]
            metodo = f"tipo {score_tipo:.2f}"
        else:
            metodo = f"sem match ({score:.2f})"

    if tipo == "INSUMO" and product_id is None:
        achado, score_insumo, _ = refs.produtos_insumo.buscar(base, limite=0.70, margem=0.03)
        if achado:
            product_id = int(achado["id"])
            type_id = type_id or (int(achado["type_id"]) if achado.get("type_id") else None)
            referencia = referencia or achado["name"]
            metodo = f"{metodo}/produto insumo {score_insumo:.2f}"

    if tipo == "INSUMO":
        # Convenção do sistema: insumo tem `product_id` e NÃO tem `type_id`
        # (que é "Tipo de Equipamento"). `type_id` em insumo esconde o item
        # da lista de INSUMOS e o empurra para a lista de EQUIPAMENTOS.
        type_id = None

    classificacao = ClassificacaoItem(
        tipo=tipo, product_id=product_id, type_id=type_id, referencia=referencia,
        similaridade=round(score, 4), metodo=metodo,
    )
    cache[chave] = classificacao
    return classificacao


# ---------------------------------------------------------------------------
# Leitura da planilha
# ---------------------------------------------------------------------------
COLUNAS_OBRIGATORIAS = (
    "Solicitado Por", "Data", "Previsão", "Envio", "Cliente", "Cidade",
    "Etiquetas", "Qtd Sol", "Equipamento | Insumos", "Substituição ou Novo",
    "PAT", "Qtd Entregue", "Data Separação", "Separado por", "Observação",
    "Req", "Ofício",
)


def ler_planilha(arquivo: Path) -> pd.DataFrame:
    df = pd.read_csv(arquivo, encoding="utf-8-sig", dtype=str).fillna("")
    df.columns = [str(c).strip() for c in df.columns]
    faltantes = [c for c in COLUNAS_OBRIGATORIAS if c not in df.columns]
    if faltantes:
        raise SystemExit(f"❌ Colunas ausentes em {arquivo}: {', '.join(faltantes)}")
    for coluna in df.columns:
        df[coluna] = df[coluna].astype(str).str.strip()
    return df


def filtrar_clientes_ignorados(
        df: pd.DataFrame, auditoria: list[dict]) -> pd.DataFrame:
    """Remove da planilha as linhas dos clientes que não devem ser migrados.

    O índice original é preservado para que a numeração `linha` da auditoria
    continue batendo com o CSV de origem. Cada linha removida vira um registro
    IGNORADA na auditoria, para a exclusão ficar rastreável.
    """
    alvo = df["Cliente"].map(normalizar).isin(
        {normalizar(nome) for nome in CLIENTES_IGNORADOS_REQUISICOES_2026})
    total = int(alvo.sum())
    if not total:
        return df

    for indice, row in df[alvo].iterrows():
        auditoria.append({
            "linha": indice + 2,
            "resultado": "IGNORADA",
            "motivo": f"Cliente ignorado nesta migração: {row['Cliente']}",
            "cliente": row["Cliente"],
            "item": row["Equipamento | Insumos"],
            "req": row["Req"],
        })

    restantes = df[~alvo].copy()
    nomes = ", ".join(sorted(CLIENTES_IGNORADOS_REQUISICOES_2026))
    print(f"   Linhas ignoradas por cliente ({nomes}): {total:,} "
          f"(restam {len(restantes):,})")
    return restantes


def filtrar_req(df: pd.DataFrame, auditoria: list[dict]) -> pd.DataFrame:
    """Mantém apenas as linhas de `Req` Separado e Parcial.

    As demais situações (ATENDIDA, Cancelada, ENVIADO, Solicitado Envio,
    Sem estoque e vazio) não geram requisição/movimento nesta migração e viram
    registro IGNORADA na auditoria, com o valor original.
    """
    reqs = df["Req"].map(normalizar)
    manter = reqs.isin(REQS_MIGRADAS)
    for indice, row in df[~manter].iterrows():
        valor = str(row["Req"]).strip() or "vazio"
        auditoria.append({
            "linha": indice + 2,
            "resultado": "IGNORADA",
            "motivo": f"Req «{valor}» fora da migração (só Separado e Parcial entram)",
            "cliente": row["Cliente"],
            "item": row["Equipamento | Insumos"],
            "pat": row["PAT"],
            "req": row["Req"],
        })

    restantes = df[manter].copy()
    removidas = int((~manter).sum())
    if removidas:
        por_req = reqs[~manter].fillna("VAZIO").replace("", "VAZIO").value_counts()
        detalhe = " | ".join(f"{k or 'VAZIO'}: {v}" for k, v in por_req.items())
        print(f"   Linhas fora por Req: {removidas:,} ({detalhe}) "
              f"(restam {len(restantes):,})")
    return restantes


def texto_substituicao(valor: object) -> str:
    """Texto descritivo da coluna `Substituição ou Novo` (ex.: REVISADO).

    `NOVO` é o caso normal de requisição avulsa e números já viram
    "Substituição de ...", então só os textos livres são devolvidos.
    """
    bruto = str(valor or "").strip()
    if not bruto or normalizar(bruto) == "NOVO" or numeros_substituicao(bruto):
        return ""
    return bruto


def numeros_substituicao(valor: object) -> tuple[int, ...]:
    """Só aceita número único ou lista de números separados por , ; /.

    Evita tratar textos como `AA061209` ou `3kg` como equipamento substituído.
    """
    bruto = str(valor or "").strip()
    if not bruto:
        return ()
    partes = [p.strip() for p in re.split(r"[,;/]", bruto)]
    validas = [p for p in partes if re.fullmatch(r"\d{2,}(\.0)?", p)]
    if len(validas) != len([p for p in partes if p]):
        return ()
    return tuple(int(float(p)) for p in validas)


# ---------------------------------------------------------------------------
# Linhas e agrupamento
# ---------------------------------------------------------------------------
@dataclass
class Linha:
    planilha: int
    cliente_id: int
    cliente: str
    cliente_score: float
    cliente_metodo: str
    cliente_legado: str
    address_id: int | None
    address_alias: str
    usuario_id: int
    solicitante: str
    organizacao: int
    etiqueta: str
    envio: int
    data: datetime
    previsao: datetime
    req: str
    oficio: str
    observacao: str
    alias: str
    classificacao: ClassificacaoItem
    quantidade: int
    entregue: int
    categoria: str
    antigos: tuple[int, ...]
    antigos_ids: tuple[int, ...]
    substituicao: str
    pat_numero: int | None
    pat_id: int | None
    separado_em: datetime | None
    separador_id: int | None
    separador: str
    motivo_movimento: str = ""


@dataclass
class Item:
    alias: str
    classificacao: ClassificacaoItem
    linhas: list[Linha]
    quantidade: int
    entregue: int
    is_exchange: bool
    antigos: tuple[int, ...]
    equipment_id: int | None
    address_id: int | None


@dataclass
class Cabecalho:
    cliente_id: int
    usuario_id: int
    organizacao: int
    envio: int
    data: datetime
    previsao: datetime
    status_id: int
    situation: str
    movement_type_id: int
    details: str
    itens: list[Item]
    linhas: list[Linha]


def entregas_por_item(linhas: list[Linha]) -> dict[str, int]:
    """Quantidade efetivamente entregue por item (uma por movimento criado).

    Espelha o agrupamento de `criar`: cada item de movimento é o par
    (alias, PAT) dentro de um grupo (data de separação, separador); o mesmo par
    repetido no mesmo movimento conta uma vez. Insumo não gera movimento,
    então nunca conta como entregue.
    """
    grupos: dict[tuple, set] = defaultdict(set)
    for linha in linhas:
        if (linha.classificacao.tipo == "EQUIPAMENTO"
                and linha.separado_em and linha.separador_id
                and linha.pat_id and not linha.motivo_movimento):
            grupos[(linha.separado_em, linha.separador_id)].add(
                (normalizar(linha.alias), linha.pat_id))
    entregas: dict[str, int] = defaultdict(int)
    for pares in grupos.values():
        for alias, _patrimonio in pares:
            entregas[alias] += 1
    return entregas


def status_do_cabecalho(linhas: list[Linha],
                        itens: list[Item]) -> tuple[int, str]:
    """Status pelo atendimento real (movimentos), não pela coluna Req.

    1 = NÃO ATENDIDA (nenhum item entregue), 2 = PARCIALMENTE ATENDIDA e
    3 = TOTALMENTE ATENDIDA (todo item com a quantidade pedida entregue).
    Insumo não gera movimento, então conta como não entregue: uma requisição
    só de insumos fica em 1 até que algo seja efetivamente entregue.
    """
    if {linha.req for linha in linhas} == {"CANCELADA"}:
        return 4, "cancelled"
    entregas = entregas_por_item(linhas)
    total_entregue = 0
    completos = 0
    for item in itens:
        entregue = min(entregas.get(normalizar(item.alias), 0),
                       int(item.quantidade or 0))
        total_entregue += entregue
        if entregue >= int(item.quantidade or 0):
            completos += 1
    if total_entregue == 0:
        return 1, "approved"
    if itens and completos == len(itens):
        return 3, "approved"
    return 2, "approved"


def detalhes_cabecalho(linhas: list[Linha]) -> str:
    partes = [MARCADOR]
    reqs = sorted({linha.req for linha in linhas if linha.req})
    if reqs:
        partes.append("Req: " + " / ".join(reqs))
    oficios = sorted({linha.oficio for linha in linhas if linha.oficio})
    if oficios:
        partes.append("Ofício: " + " / ".join(oficios[:5]))
    observacoes = []
    for linha in linhas:
        if linha.observacao and linha.observacao not in observacoes:
            observacoes.append(linha.observacao)
    if observacoes:
        partes.append("Obs: " + " | ".join(observacoes))
    return " | ".join(partes)[:4000]


def agrupar(linhas: list[Linha]) -> list[Cabecalho]:
    grupos: dict[tuple, list[Linha]] = defaultdict(list)
    for linha in linhas:
        grupos[(linha.cliente_id, linha.usuario_id, linha.organizacao,
                linha.envio, linha.data, linha.previsao)].append(linha)

    cabecalhos: list[Cabecalho] = []
    for chave, membros in grupos.items():
        cliente_id, usuario_id, organizacao, envio, data, previsao = chave
        por_item: dict[str, list[Linha]] = defaultdict(list)
        for linha in membros:
            por_item[normalizar(linha.alias)].append(linha)

        itens: list[Item] = []
        for linhas_item in por_item.values():
            somatorio = sum(l.quantidade for l in linhas_item if l.quantidade)
            quantidade = somatorio if somatorio >= len(linhas_item) else len(linhas_item)
            informado = sum(l.entregue for l in linhas_item if l.entregue)
            entregue = min(informado, quantidade) if informado else 0
            antigos = tuple(dict.fromkeys(n for l in linhas_item for n in l.antigos))
            antigos_ids = tuple(dict.fromkeys(i for l in linhas_item for i in l.antigos_ids))
            equipment_id = antigos_ids[0] if antigos_ids else next(
                (l.pat_id for l in linhas_item if l.pat_id), None)
            itens.append(Item(
                alias=linhas_item[0].alias,
                classificacao=linhas_item[0].classificacao,
                linhas=linhas_item,
                quantidade=quantidade,
                entregue=entregue,
                is_exchange=bool(antigos),
                antigos=antigos,
                equipment_id=equipment_id,
                address_id=next(
                    (l.address_id for l in linhas_item if l.address_id), None),
            ))

        status_id, situation = status_do_cabecalho(membros, itens)
        movement_type_id = (TIPOS_MOVIMENTO["SUBSTITUICAO"]
                            if all(item.is_exchange for item in itens)
                            else TIPOS_MOVIMENTO["AVULSO"])
        cabecalhos.append(Cabecalho(
            cliente_id=cliente_id, usuario_id=usuario_id, organizacao=organizacao,
            envio=envio, data=data, previsao=previsao, status_id=status_id,
            situation=situation, movement_type_id=movement_type_id,
            details=detalhes_cabecalho(membros), itens=itens, linhas=membros,
        ))
    cabecalhos.sort(key=lambda c: (c.data or datetime.min, c.cliente_id, c.usuario_id))
    return cabecalhos

# ---------------------------------------------------------------------------
# Validação linha a linha
# ---------------------------------------------------------------------------
COLUNAS_AUDITORIA = (
    "linha", "resultado", "motivo", "cliente", "cliente_id", "similaridade_cliente",
    "metodo_cliente", "cliente_legado", "address_id", "address_alias_endereco",
    "etiqueta", "organizacao_id", "solicitante", "usuario_id", "item", "tipo_item",
    "produto_id", "type_id", "similaridade_item", "metodo_item", "categoria",
    "equipamentos_antigos", "pat", "pat_id", "separador", "separador_id",
    "data_separacao", "req", "status_id", "situacao", "observacao",
)


def montar_linha(row, indice: int, refs: Referencias,
                 cache: dict[str, ClassificacaoItem],
                 auditoria: list[dict]) -> Linha | None:
    planilha = indice + 2
    auditoria_linha = {
        "linha": planilha, "cliente": row["Cliente"], "etiqueta": row["Etiquetas"],
        "solicitante": row["Solicitado Por"], "item": row["Equipamento | Insumos"],
        "req": row["Req"], "pat": row["PAT"], "separador": row["Separado por"],
        "observacao": row["Observação"],
    }

    def ignorar(motivo: str):
        auditoria.append({**auditoria_linha, "resultado": "IGNORADA", "motivo": motivo})
        return None

    data = data_planilha(row["Data"])
    # Algumas linhas chegam sem `Data` mas com `Data Separação` preenchida
    # (CIRETRAN/MA-BACABAL). Nesses casos a separação serve de data da requisição.
    if not data:
        data = data_planilha(row["Data Separação"])
        if not data:
            return ignorar("Data da requisição vazia ou inválida")
        data_origem_separacao = True
    else:
        data_origem_separacao = False

    cliente, score_cliente, sugestoes = refs.clientes.buscar(
        row["Cliente"], row["Cidade"])
    if not cliente:
        nomes = " | ".join(
            f"{s.get('legacy_name') or '?'} -> {s.get('customer_name') or 'sem cliente'}"
            for s in sugestoes[:3])
        return ignorar(
            f"Cliente sem match confiável (similaridade {score_cliente:.2f}); "
            f"sugestões: {nomes}")

    organizacao = selecionar_organizacao(row["Etiquetas"])
    if not organizacao or organizacao not in refs.organizacoes:
        return ignorar(f"Etiqueta sem organização reconhecida: {row['Etiquetas']}")

    envio = TRANSPORTES.get(normalizar(row["Envio"]))
    avisos: list[str] = []
    if data_origem_separacao:
        avisos.append("Data vazia; usada Data Separação como data da requisição")
    if not envio:
        if not normalizar(row["Envio"]):
            envio = TRANSPORTES["ROTA"]
            avisos.append("Envio vazio; assumido Rota")
        else:
            return ignorar(f"Transporte não mapeado: {row['Envio']}")
    if envio not in refs.transportes:
        return ignorar(f"Transporte {envio} não existe no destino")

    username = USUARIOS.get(normalizar(row["Solicitado Por"]))
    if not username:
        username = USUARIO_PADRAO
        avisos.append(f"solicitante '{row['Solicitado Por'] or 'vazio'}' sem usuário; usado {USUARIO_PADRAO}")
    usuario_id = refs.usuarios.get(normalizar(username))
    if not usuario_id:
        return ignorar(f"Usuário {username} não encontrado no destino")

    classificacao = classificar_item(row["Equipamento | Insumos"], refs, cache)
    if classificacao.tipo == "INDEFINIDO":
        avisos.append("item sem tipo/produto correspondente")

    substituicao = row["Substituição ou Novo"]
    # Insumo não substitui equipamento: os números dessa coluna são códigos de
    # produto (ex.: 19978-1/2) e não podem virar tombo/`is_exchange`.
    antigos_brutos = (numeros_substituicao(substituicao)
                      if classificacao.tipo == "EQUIPAMENTO" else ())
    antigos: tuple[int, ...] = ()
    antigos_ids: list[int] = []
    if antigos_brutos:
        # Só é substituição quando TODOS os números existem em equipments.number.
        encontrados: list[int] = []
        faltando: list[int] = []
        for numero_antigo in antigos_brutos:
            candidatos = refs.equipamentos.get(numero_antigo, [])
            if not candidatos:
                faltando.append(numero_antigo)
                continue
            escolhido = next((c for c in candidatos if c["organizacao"] == organizacao),
                             candidatos[0])
            encontrados.append(escolhido["id"])
        if faltando:
            avisos.append(
                "substituição ignorada: tombo(s) "
                + ", ".join(str(n) for n in faltando)
                + " não encontrado(s) em equipments.number")
        else:
            antigos = antigos_brutos
            antigos_ids = encontrados

    pat_numero = numero_pat(row["PAT"])
    pat_id = None
    pat_status_id = None
    if pat_numero:
        candidatos = refs.equipamentos.get(pat_numero, [])
        if candidatos:
            escolhido = next((c for c in candidatos if c["organizacao"] == organizacao),
                             candidatos[0])
            pat_id = escolhido["id"]
            pat_status_id = escolhido["status_id"]
        else:
            avisos.append(f"PAT {pat_numero} não encontrado em equipments")

    req_normalizada = normalizar(row["Req"])
    if req_normalizada == "PARCIAL" and classificacao.tipo == "EQUIPAMENTO":
        # Parcial só entra com o equipamento do PAT disponível para locação.
        # Insumo não tem equipamento a entregar: a linha abre apenas o item da
        # requisição (sem movimento), como nas linhas Separado.
        if not pat_id:
            return ignorar(
                f"Parcial sem equipamento: PAT «{row['PAT']}» não localizado")
        if pat_status_id != STATUS_DISPONIVEL_LOCACAO:
            return ignorar(
                f"Parcial: equipamento {pat_numero} indisponível para locação "
                f"(status_id {pat_status_id})")

    separador_nome = row["Separado por"]
    separador_username = SEPARADORES.get(normalizar(separador_nome)) if separador_nome else None
    separador_id = refs.usuarios.get(normalizar(separador_username)) if separador_username else None

    separado_em = data_planilha(row["Data Separação"])
    motivo_movimento = ""
    if classificacao.tipo != "EQUIPAMENTO":
        # Insumo (ou item sem classificação de equipamento) entra apenas como
        # requisição: o PAT dessas linhas é código do produto, não tombo.
        avisos.append("insumo/sem patrimônio: apenas requisição, sem movimento")
    elif not separado_em:
        motivo_movimento = "Data Separação ausente"
    elif not pat_id:
        motivo_movimento = "PAT ausente ou não encontrado"
    else:
        # Separador não mapeado (ou vazio): usa o usuário padrão (admin) em vez
        # de bloquear o movimento.
        if not separador_id:
            separador_id = refs.usuarios.get(normalizar(USUARIO_PADRAO))
            avisos.append(
                f"separador '{separador_nome or 'vazio'}' sem usuário; "
                f"usado {USUARIO_PADRAO}")
        if not separador_id:
            motivo_movimento = f"separador sem usuário: {separador_nome or 'vazio'}"

    quantidade = numero(row["Qtd Sol"]) or 0
    entregue = numero(row["Qtd Entregue"]) or 0

    if not cliente.address_id:
        avisos.append("cliente sem endereço migrado (item ficará sem address_id)")
    if cliente.metodo == "similaridade" and score_cliente < 0.85:
        avisos.append(f"confirmar cliente (similaridade {score_cliente:.2f})")

    auditoria.append({
        **auditoria_linha,
        "resultado": "MOVIMENTO BLOQUEADO" if motivo_movimento else "OK",
        "motivo": "; ".join(avisos + ([motivo_movimento] if motivo_movimento else [])),
        "cliente_id": cliente.customer_id,
        "similaridade_cliente": round(score_cliente, 4),
        "metodo_cliente": cliente.metodo,
        "cliente_legado": cliente.legacy_name,
        "address_id": cliente.address_id,
        "address_alias_endereco": cliente.address_alias,
        "organizacao_id": organizacao, "usuario_id": usuario_id,
        "tipo_item": classificacao.tipo, "produto_id": classificacao.product_id,
        "type_id": classificacao.type_id, "similaridade_item": classificacao.similaridade,
        "metodo_item": classificacao.metodo,
        "categoria": "SUBSTITUICAO" if antigos else "AVULSO",
        "equipamentos_antigos": ",".join(str(n) for n in antigos),
        "pat_id": pat_id, "separador_id": separador_id,
        "data_separacao": separado_em.date().isoformat() if separado_em else "",
    })

    return Linha(
        planilha=planilha,
        cliente_id=cliente.customer_id, cliente=cliente.customer_name,
        cliente_score=round(score_cliente, 4),
        cliente_metodo=cliente.metodo, cliente_legado=cliente.legacy_name,
        address_id=cliente.address_id, address_alias=cliente.address_alias,
        usuario_id=usuario_id, solicitante=username,
        organizacao=organizacao, etiqueta=row["Etiquetas"], envio=envio,
        data=data, previsao=data_planilha(row["Previsão"]) or data,
        req=normalizar(row["Req"]), oficio=row["Ofício"],
        observacao=row["Observação"],
        alias=row["Equipamento | Insumos"], classificacao=classificacao,
        quantidade=quantidade, entregue=entregue,
        categoria="SUBSTITUICAO" if antigos else "AVULSO",
        antigos=antigos, antigos_ids=tuple(antigos_ids),
        substituicao=substituicao,
        pat_numero=pat_numero, pat_id=pat_id,
        separado_em=separado_em, separador_id=separador_id,
        separador=separador_nome, motivo_movimento=motivo_movimento,
    )


def preparar(engine, arquivo: Path = ARQUIVO_FONTE,
             engine_legado=None) -> tuple[list[Cabecalho], list[dict]]:
    print("\n" + "=" * 72)
    print("📦 MÓDULO: MIGRAÇÃO DE REQUISIÇÕES 2026")
    print("=" * 72)
    print(f"📖 Lendo arquivo de origem: {arquivo}")
    df = ler_planilha(arquivo)
    print(f"   Linhas carregadas: {len(df):,}")
    auditoria: list[dict] = []
    df = filtrar_clientes_ignorados(df, auditoria)
    df = filtrar_req(df, auditoria)

    numeros_necessarios: set[int] = set()
    for valor in df["PAT"]:
        achado = numero_pat(valor)
        if achado:
            numeros_necessarios.add(achado)
    for valor in df["Substituição ou Novo"]:
        numeros_necessarios.update(numeros_substituicao(valor))

    with engine.connect() as conn:
        with engine_legado.connect() as conn_legado:
            refs = carregar_referencias(conn, conn_legado)
        refs.equipamentos = carregar_equipamentos(conn, numeros_necessarios)

    print(
        f"   Referências: {len(refs.clientes.registros):,} clientes do legado, "
        f"{len(refs.produtos.registros):,} produtos, "
        f"{len(refs.tipos.registros):,} tipos, {len(refs.usuarios)} usuários, "
        f"{len(refs.equipamentos):,} números de equipamento localizados."
    )

    linhas: list[Linha] = []
    cache: dict[str, ClassificacaoItem] = {}
    for indice, row in tqdm(df.iterrows(), total=len(df),
                            desc="Validando linhas", unit="linha"):
        linha = montar_linha(row, indice, refs, cache, auditoria)
        if linha:
            linhas.append(linha)

    print(f"\n   ✅ Linhas elegíveis: {len(linhas):,}")
    print(f"   ⚠️ Linhas ignoradas/bloqueadas: {sum(1 for a in auditoria if a['resultado'] != 'OK'):,}")

    cabecalhos = agrupar(linhas)
    # A auditoria nasce por linha, antes do agrupamento; o status final é do
    # cabeçalho (calculado pelo atendimento real). Propaga para as linhas para
    # que o relatório não mostre um status que a requisição não recebeu.
    status_por_linha = {
        linha.planilha: (cabecalho.status_id, cabecalho.situation)
        for cabecalho in cabecalhos for linha in cabecalho.linhas
    }
    for registro in auditoria:
        final = status_por_linha.get(registro.get("linha"))
        if final:
            registro["status_id"], registro["situacao"] = final
    return cabecalhos, auditoria

# ---------------------------------------------------------------------------
# Gravação
# ---------------------------------------------------------------------------
def departamento_do_item(equipamento: bool) -> str:
    """Departamento pelo tipo do item (classificado pelo nome).

    Item EQUIPAMENTO é separado na EXPEDIÇÃO; insumo fica com a ELISANGELA.
    O ALMOXARIFADO não é usado na importação de requisições. A decisão não usa
    a presença de PAT: linha de insumo pode trazer código do produto na coluna.
    """
    return DEPARTAMENTO_EQUIPAMENTO if equipamento else DEPARTAMENTO_PECAS_ADI


def carregar_departamentos(conn) -> dict[str, int]:
    """Nome -> id em `service_order_departments` (falha se faltar algum)."""
    mapa = {
        str(nome).strip().upper(): int(departamento_id)
        for nome, departamento_id in conn.execute(text(
            "SELECT `name`, `id` FROM `service_order_departments` "
            "WHERE `deleted_at` IS NULL"))
    }
    faltando = [nome for nome in (DEPARTAMENTO_EQUIPAMENTO,
                                  DEPARTAMENTO_PECAS_ADI) if nome not in mapa]
    if faltando:
        raise RuntimeError(
            "Departamentos ausentes em service_order_departments: "
            + ", ".join(faltando))
    return mapa


def proximos_numeros(base: int, quantidade: int) -> list[int]:
    """Protocolos contíguos acima de `base` (o número corrente do banco)."""
    base, quantidade = int(base or 0), int(quantidade or 0)
    if quantidade <= 0:
        return []
    return list(range(base + 1, base + quantidade + 1))


def sincronizar_sequencia(conn, ultimo: int) -> None:
    """Alinha `movement_number_sequences.last_number` com o maior protocolo.

    A sequência não é truncada pelo reset e o INSERT em SQL puro não passa pelo
    hook do Laravel, então sem esta sincronização o próximo movimento criado
    pela tela reutilizaria um número.
    """
    existe = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'movement_number_sequences'")).scalar()
    if not existe:
        return
    alterado = conn.execute(text(
        "UPDATE `movement_number_sequences` "
        "SET `last_number` = GREATEST(`last_number`, :ultimo) WHERE `id` = 1"),
        {"ultimo": int(ultimo)})
    if alterado.rowcount == 0:
        conn.execute(text(
            "INSERT INTO `movement_number_sequences` (`id`, `last_number`) "
            "VALUES (1, :ultimo)"), {"ultimo": int(ultimo)})


def numerar_movimentos_sem_protocolo(conn) -> list[tuple[int, str]]:
    """Dá protocolo aos movimentos com `number` NULL/vazio, por ordem de id.

    Regra do importador legado: nunca usar `number = id` (os ids dos movimentos
    de requisição são menores que o maior número já emitido e colidiriam).
    A numeração é sempre acima do MAX(number) existente e cobre também o
    movimento avulso criado pelos ajustes.
    """
    ids = conn.execute(text(
        "SELECT `id` FROM `movements` "
        "WHERE `number` IS NULL OR `number` = '' ORDER BY `id`")).scalars().all()
    base = int(conn.execute(text(
        "SELECT COALESCE(MAX(CAST(`number` AS UNSIGNED)), 0) FROM `movements` "
        "WHERE `number` IS NOT NULL AND `number` <> ''")).scalar() or 0)
    atribuidos: list[tuple[int, str]] = []
    for movimento_id, numero_novo in zip(ids, proximos_numeros(base, len(ids))):
        conn.execute(text("UPDATE `movements` SET `number` = :numero WHERE `id` = :id"),
                     {"numero": str(numero_novo), "id": movimento_id})
        atribuidos.append((movimento_id, str(numero_novo)))
    if atribuidos:
        sincronizar_sequencia(conn, int(atribuidos[-1][1]))
    return atribuidos


def criar(engine, cabecalhos: list[Cabecalho]) -> tuple[dict, Path]:
    stats = defaultdict(int)
    log_rows: list[dict] = []
    print(f"\n📝 Gravando {len(cabecalhos):,} requisições em transação única...")
    with engine.begin() as conn:
        departamentos = carregar_departamentos(conn)
        for cabecalho in tqdm(cabecalhos, desc="Gravando requisições", unit="req"):
            ordem = conn.execute(text("""
                INSERT INTO service_orders
                    (status_id, movement_type_id, contract_id, user_id, mode_transport_id,
                     requires_invoice, organization_id, recipient_customer_id,
                     maintenance_created_order, deadline, situation, details,
                     created_at, updated_at)
                VALUES
                    (:status_id, :movement_type_id, NULL, :user_id, :envio,
                     0, :organizacao, :cliente_id,
                     0, :deadline, :situation, :details,
                     :created, :created)
            """), {
                "status_id": cabecalho.status_id,
                "movement_type_id": cabecalho.movement_type_id,
                "user_id": cabecalho.usuario_id,
                "envio": cabecalho.envio,
                "organizacao": cabecalho.organizacao,
                "cliente_id": cabecalho.cliente_id,
                "deadline": cabecalho.previsao,
                "situation": cabecalho.situation,
                "details": cabecalho.details,
                "created": cabecalho.data,
            })
            ordem_id = ordem.lastrowid
            stats["requisicoes"] += 1

            item_ids: dict[str, int] = {}
            for item in cabecalho.itens:
                classificacao = item.classificacao
                # Departamento pelo tipo do item (nome), não pela presença de
                # PAT: insumo sempre ELISANGELA, equipamento sempre EXPEDIÇÃO.
                departamento = departamento_do_item(
                    classificacao.tipo == "EQUIPAMENTO")
                insumo = classificacao.tipo == "INSUMO"
                sem_estoque = any(l.req == "SEM ESTOQUE" for l in item.linhas)
                partes_detalhe = [classificacao.tipo.title(), classificacao.metodo]
                if item.antigos:
                    partes_detalhe.append(
                        "Substituição de " + ", ".join(str(n) for n in item.antigos))
                textos_substituicao = sorted({
                    texto for texto in (texto_substituicao(l.substituicao)
                                        for l in item.linhas) if texto}
                ) if classificacao.tipo == "EQUIPAMENTO" else []
                if textos_substituicao:
                    partes_detalhe.append(
                        "Substituição informada: " + ", ".join(textos_substituicao))
                detalhe_item = " | ".join(p for p in partes_detalhe if p)
                criado = conn.execute(text("""
                    INSERT INTO service_order_items
                        (status_id, service_order_id, department_id, movement_type_id,
                         contract_item_id, address_id, alias, equipment_id, type_id,
                         product_id, is_exchange, is_extra, quantity_product,
                         fulfilled_quantity_product, quantity, fulfilled_quantity,
                         details, stock_status, created_at, updated_at)
                    VALUES
                        (:status_id, :ordem_id, :departamento, :movement_type,
                         NULL, :address_id, :alias, :equipment_id, :type_id,
                         :product_id, :is_exchange, 0, :qtd_produto,
                         :entregue_produto, :quantity, :fulfilled,
                         :details, :stock_status, :created, :created)
                """), {
                    "status_id": cabecalho.status_id,
                    "ordem_id": ordem_id,
                    "departamento": departamentos[departamento],
                    # Tipo por item: substituição só quando o tombo da coluna
                    # "Substituição ou Novo" existe em equipments.number.
                    "movement_type": (TIPOS_MOVIMENTO["SUBSTITUICAO"]
                                      if item.is_exchange
                                      and classificacao.tipo == "EQUIPAMENTO"
                                      else TIPOS_MOVIMENTO["AVULSO"]),
                    "address_id": item.address_id,
                    "alias": item.alias[:255],
                    "equipment_id": (item.equipment_id
                                     if classificacao.tipo == "EQUIPAMENTO"
                                     else None),
                    "type_id": classificacao.type_id,
                    "product_id": classificacao.product_id,
                    "is_exchange": int(item.is_exchange),
                    "qtd_produto": item.quantidade if insumo else None,
                    "entregue_produto": item.entregue if insumo else 0,
                    "quantity": item.quantidade,
                    "fulfilled": item.entregue,
                    "details": detalhe_item,
                    "stock_status": "awaiting_purchase" if sem_estoque else "in_stock",
                    "created": cabecalho.data,
                })
                item_ids[normalizar(item.alias)] = criado.lastrowid
                stats["itens"] += 1
                stats[f"itens_{classificacao.tipo.lower()}"] += 1
                stats[f"itens_departamento_{departamento.lower()}"] += 1

            movimentos: dict[tuple, list[Linha]] = defaultdict(list)
            for linha in cabecalho.linhas:
                if (linha.classificacao.tipo == "EQUIPAMENTO"
                        and linha.separado_em and linha.separador_id
                        and linha.pat_id):
                    movimentos[(linha.separado_em, linha.separador_id)].append(linha)

            for (data_movimento, separador_id), linhas_mov in movimentos.items():
                antigos = sorted({n for l in linhas_mov for n in l.antigos})
                detalhe = f"{MARCADOR} | requisição {ordem_id}"
                if antigos:
                    detalhe += " | substituição de " + ", ".join(str(n) for n in antigos)
                movimento = conn.execute(text("""
                    INSERT INTO movements
                        (number, movement_date, service_order_id, recipient_customer_id,
                         organization_id, status_id, created_by, details,
                         maintenance_created_movement, created_at, updated_at)
                    VALUES
                        (NULL, :data, :ordem_id, :cliente_id, :organizacao, 3,
                         :created_by, :details, 0, :data, :data)
                """), {
                    "data": data_movimento, "ordem_id": ordem_id,
                    "cliente_id": cabecalho.cliente_id,
                    "organizacao": cabecalho.organizacao,
                    "created_by": separador_id, "details": detalhe,
                })
                movimento_id = movimento.lastrowid
                stats["movimentos"] += 1

                vistos: set[tuple[int, int]] = set()
                for linha in linhas_mov:
                    item_id = item_ids.get(normalizar(linha.alias))
                    if not item_id:
                        continue
                    chave = (item_id, linha.pat_id)
                    if chave in vistos:
                        continue
                    vistos.add(chave)
                    operacao = "SUBSTITUIÇÃO" if linha.antigos else "AVULSO"
                    tipo_linha = (TIPOS_MOVIMENTO["SUBSTITUICAO"]
                                  if linha.antigos
                                  else TIPOS_MOVIMENTO["AVULSO"])
                    conn.execute(text("""
                        INSERT INTO movement_items
                            (movement_id, movement_type_id, service_order_item_id,
                             equipment_id, status_id, alias, old_organization_id,
                             new_organization_id, operation_type, created_at, updated_at)
                        VALUES
                            (:movimento_id, :movement_type, :item_id, :equipment_id,
                             3, :alias, NULL, :organizacao, :operacao, :data, :data)
                    """), {
                        "movimento_id": movimento_id,
                        "movement_type": tipo_linha,
                        "item_id": item_id,
                        "equipment_id": linha.pat_id,
                        "alias": linha.alias[:255],
                        "organizacao": cabecalho.organizacao,
                        "operacao": operacao,
                        "data": data_movimento,
                    })
                    stats["movimentos_itens"] += 1

                log_rows.append({
                    "requisicao_id": ordem_id,
                    "cliente_id": cabecalho.cliente_id,
                    "cliente": cabecalho.linhas[0].cliente,
                    "data_requisicao": cabecalho.data.date().isoformat(),
                    "prazo": cabecalho.previsao.date().isoformat(),
                    "org_id": cabecalho.organizacao,
                    "solicitante_id": cabecalho.usuario_id,
                    "movimento_id": movimento_id,
                    "data_movimento": data_movimento.date().isoformat(),
                    "separador_id": separador_id,
                    "patrimonios": ",".join(str(l.pat_numero) for l in linhas_mov),
                    "equipamentos_antigos": ",".join(str(n) for n in antigos),
                })

        # Protocolo dos movimentos (o INSERT em SQL puro não passa pelo hook
        # do Laravel, então a numeração é feita aqui, ao final da transação).
        numerados = numerar_movimentos_sem_protocolo(conn)
        stats["movimentos_numerados"] = len(numerados)
        protocolos = dict(numerados)
        for linha in log_rows:
            protocolo = protocolos.get(linha["movimento_id"])
            if protocolo:
                linha["protocolo"] = protocolo

    return dict(stats), salvar_log(log_rows)


def salvar_auditoria(auditoria: list[dict], destino: Path) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=COLUNAS_AUDITORIA, extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(auditoria)
    return destino


COLUNAS_NAO_MIGRADAS = (
    "linha", "resultado", "motivo", "req", "cliente", "item", "pat",
    "pat_id", "data_separacao",
)


def salvar_relatorio_nao_migradas(
        auditoria: list[dict], destino: Path = ARQUIVO_NAO_MIGRADAS) -> Path:
    """Relatório do que ficou de fora da migração e por quê.

    Inclui linhas ignoradas (Req fora, cliente sem match, Parcial sem
    equipamento disponível, dados obrigatórios ausentes) e itens cujo movimento
    não pôde ser criado (PAT ausente, data de separação, separador sem usuário).
    """
    linhas = [registro for registro in auditoria
              if registro.get("resultado") != "OK"]
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=COLUNAS_NAO_MIGRADAS,
                                  extrasaction="ignore")
        escritor.writeheader()
        escritor.writerows(linhas)

    print(f"\n📄 Não migradas: {len(linhas):,} linha(s) -> {destino}")
    if linhas:
        motivos = defaultdict(int)
        for registro in linhas:
            motivos[re.sub(r"\d+", "N", registro["motivo"])[:90]] += 1
        for motivo, total in sorted(motivos.items(), key=lambda kv: -kv[1])[:12]:
            print(f"      {total:>5,}  {motivo}")
    return destino


def salvar_log(log_rows: list[dict]) -> Path:
    DIR_LOGS.mkdir(parents=True, exist_ok=True)
    destino = DIR_LOGS / f"requisicoes_2026_criadas_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
    if log_rows:
        pd.DataFrame(log_rows).to_csv(destino, index=False, encoding="utf-8-sig")
    return destino


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------
def imprimir_resumo(cabecalhos: list[Cabecalho], auditoria: list[dict]) -> None:
    itens = [item for cab in cabecalhos for item in cab.itens]
    por_tipo: dict[str, int] = defaultdict(int)
    for item in itens:
        por_tipo[item.classificacao.tipo] += 1

    movimentos = 0
    itens_movimento = 0
    for cab in cabecalhos:
        grupos: dict[tuple, set] = defaultdict(set)
        for linha in cab.linhas:
            if (linha.classificacao.tipo == "EQUIPAMENTO"
                    and linha.separado_em and linha.separador_id
                    and linha.pat_id and not linha.motivo_movimento):
                grupos[(linha.separado_em, linha.separador_id)].add(
                    (normalizar(linha.alias), linha.pat_id))
        movimentos += len(grupos)
        itens_movimento += sum(len(v) for v in grupos.values())

    substituicoes = sum(1 for l in (l for c in cabecalhos for l in c.linhas)
                        if l.antigos)
    bloqueios = [a for a in auditoria if a["resultado"] == "MOVIMENTO BLOQUEADO"]
    ignoradas = [a for a in auditoria if a["resultado"] == "IGNORADA"]
    a_confirmar = [a for a in auditoria
                   if a.get("metodo_cliente") == "similaridade"
                   and a.get("similaridade_cliente", 1) < 0.85
                   and a["resultado"] != "IGNORADA"]

    print("\n📊 RESUMO")
    print(f"   Requisições (cabeçalhos): {len(cabecalhos):,}")
    print(f"   Itens: {len(itens):,}  (equipamento {por_tipo['EQUIPAMENTO']:,} | "
          f"insumo {por_tipo['INSUMO']:,} | indefinido {por_tipo['INDEFINIDO']:,})")
    por_departamento: dict[str, int] = defaultdict(int)
    for item in itens:
        equipamento = item.classificacao.tipo == "EQUIPAMENTO"
        por_departamento[
            DEPARTAMENTO_EQUIPAMENTO if equipamento
            else DEPARTAMENTO_PECAS_ADI] += 1
    print("   Itens por departamento: " + " | ".join(
        f"{nome} {total:,}" for nome, total in sorted(por_departamento.items())))
    print(f"   Movimentos previstos: {movimentos:,} ({itens_movimento:,} itens)")
    print(f"   Linhas de substituição válida: {substituicoes:,}")
    print(f"   Linhas com separação bloqueada: {len(bloqueios):,}")
    print(f"   Linhas ignoradas: {len(ignoradas):,}")
    print(f"   Clientes com match < 0.85 (revisar): {len(a_confirmar):,}")
    status = defaultdict(int)
    for cab in cabecalhos:
        status[f"{cab.status_id}/{cab.situation}"] += 1
    print("   Status das requisições: " + " | ".join(
        f"{k}: {v:,}" for k, v in sorted(status.items())))
    motivos = defaultdict(int)
    for registro in ignoradas + bloqueios:
        motivos[re.sub(r"\d+", "N", registro["motivo"])[:80]] += 1
    if motivos:
        print("\n   Principais motivos:")
        for motivo, total in sorted(motivos.items(), key=lambda kv: -kv[1])[:12]:
            print(f"      {total:>5,}  {motivo}")


def remover_execucao_anterior(engine) -> dict[str, int]:
    """Apaga requisições/movimentos gravados por uma execução anterior.

    Só remove o que tem o marcador desta migração. Antes de apagar, confere que
    nada externo (remessas, manutenções, históricos) aponta para esses
    registros; se houver dependentes, aborta em vez de deixar órfãos.
    """
    from sqlalchemy import bindparam

    marcador = f"%{MARCADOR}%"
    with engine.begin() as conn:
        ordens = conn.execute(text(
            "SELECT id FROM service_orders WHERE details LIKE :m"),
            {"m": marcador}).scalars().all()
        movimentos = conn.execute(text(
            "SELECT id FROM movements WHERE details LIKE :m"),
            {"m": marcador}).scalars().all()
        itens = conn.execute(text(
            "SELECT id FROM service_order_items WHERE service_order_id IN "
            "(SELECT id FROM service_orders WHERE details LIKE :m)"),
            {"m": marcador}).scalars().all()
        itens_mov = conn.execute(text(
            "SELECT id FROM movement_items WHERE movement_id IN "
            "(SELECT id FROM movements WHERE details LIKE :m)"),
            {"m": marcador}).scalars().all()

        if not (ordens or movimentos):
            return {"requisicoes": 0, "itens": 0, "movimentos": 0,
                    "itens_movimento": 0}

        def contar(sql: str, ids: list[int]) -> int:
            if not ids:
                return 0
            return int(conn.execute(
                text(sql).bindparams(bindparam("ids", expanding=True)),
                {"ids": ids}).scalar_one())

        dependentes = {
            "equipment_history(movement_item)": contar(
                "SELECT COUNT(*) FROM equipment_history "
                "WHERE movement_item_id IN :ids", itens_mov),
            "equipment_history(service_order_item)": contar(
                "SELECT COUNT(*) FROM equipment_history "
                "WHERE service_order_item_id IN :ids", itens),
            "shipment_items": contar(
                "SELECT COUNT(*) FROM shipment_items WHERE movement_item_id IN :ids",
                itens_mov),
            "volume_items": contar(
                "SELECT COUNT(*) FROM volume_items WHERE movement_item_id IN :ids",
                itens_mov),
            "maintenance_items": contar(
                "SELECT COUNT(*) FROM maintenance_items WHERE movement_id IN :ids",
                movimentos),
            "return_confirmations_items": contar(
                "SELECT COUNT(*) FROM return_confirmations_items "
                "WHERE movement_id IN :ids", movimentos),
            "romaneios": contar(
                "SELECT COUNT(*) FROM romaneios WHERE movement_id IN :ids",
                movimentos),
            "shipment_movements": contar(
                "SELECT COUNT(*) FROM shipment_movements WHERE movement_id IN :ids",
                movimentos),
            "shipment_document_movements": contar(
                "SELECT COUNT(*) FROM shipment_document_movements "
                "WHERE movement_id IN :ids", movimentos),
            "shipment_documents": contar(
                "SELECT COUNT(*) FROM shipment_documents WHERE movement_id IN :ids",
                movimentos),
            "volumes": contar(
                "SELECT COUNT(*) FROM volumes WHERE movement_id IN :ids",
                movimentos),
            "service_order_accessories": contar(
                "SELECT COUNT(*) FROM service_order_accessories "
                "WHERE service_order_item_id IN :ids", itens),
            "service_order_item_extra_equipments": contar(
                "SELECT COUNT(*) FROM service_order_item_extra_equipments "
                "WHERE service_order_item_id IN :ids", itens),
        }
        achados = {k: v for k, v in dependentes.items() if v}
        if achados:
            raise RuntimeError(
                "execução anterior tem registros dependentes: "
                + ", ".join(f"{k}={v}" for k, v in achados.items()))

        def apagar(sql: str, ids: list[int]) -> int:
            if not ids:
                return 0
            return conn.execute(
                text(sql).bindparams(bindparam("ids", expanding=True)),
                {"ids": ids}).rowcount

        removidos = {
            "itens_movimento": apagar(
                "DELETE FROM movement_items WHERE movement_id IN :ids", movimentos),
            "movimentos": apagar(
                "DELETE FROM movements WHERE id IN :ids", movimentos),
            "itens": apagar(
                "DELETE FROM service_order_items WHERE service_order_id IN :ids",
                ordens),
            "requisicoes": apagar(
                "DELETE FROM service_orders WHERE id IN :ids", ordens),
        }
    return removidos


def main(argv=None, engine_new=None, engine_legado=None):
    parser = argparse.ArgumentParser(description="Migra as requisições de 2026.")
    parser.add_argument("--execute", action="store_true",
                        help="Grava no banco. Sem esta flag o script só valida (dry-run).")
    parser.add_argument("--dry-run", action="store_true", help="Força o modo de validação.")
    parser.add_argument("--arquivo", type=Path, default=ARQUIVO_FONTE)
    parser.add_argument("--auditoria", type=Path, default=ARQUIVO_AUDITORIA)
    parser.add_argument("--force", action="store_true",
                        help="Permite gravar mesmo já existindo execução anterior.")
    parser.add_argument("--replace", action="store_true",
                        help="Com --execute, apaga a importação anterior (mesmo "
                             "marcador) antes de gravar de novo.")
    argumentos = [a for a in (argv or []) if a != "--"]
    args = parser.parse_args(argumentos)

    if args.replace and (not args.execute or args.dry_run):
        parser.error("--replace exige --execute (e não combina com --dry-run)")

    if engine_new is None or engine_legado is None:
        from config.database import obter_engines
        engine_new, engine_legado = obter_engines()

    cabecalhos, auditoria = preparar(engine_new, args.arquivo, engine_legado)
    destino = salvar_auditoria(auditoria, args.auditoria)
    print(f"\n📄 Auditoria: {destino}")
    imprimir_resumo(cabecalhos, auditoria)
    salvar_relatorio_nao_migradas(auditoria)

    if not args.execute or args.dry_run:
        print("\n🔒 Dry-run: nada foi gravado. Use --execute para gravar.")
        return

    if args.replace:
        removidos = remover_execucao_anterior(engine_new)
        print("\n🧹 Execução anterior removida: " + " | ".join(
            f"{k} {v:,}" for k, v in removidos.items()))

    with engine_new.connect() as conn:
        existentes = conn.execute(text(
            "SELECT COUNT(*) FROM service_orders WHERE details LIKE :marcador"
        ), {"marcador": f"%{MARCADOR}%"}).scalar_one()
    if existentes and not args.force:
        print(f"\n⛔ Já existem {existentes:,} requisições desta migração. "
              "Nada foi gravado; use --force para duplicar mesmo assim.")
        return

    stats, log = criar(engine_new, cabecalhos)
    print("\n🏁 MIGRAÇÃO CONCLUÍDA")
    print(f"   Requisições: {stats.get('requisicoes', 0):,}")
    print(f"   Itens: {stats.get('itens', 0):,}")
    print(f"   Movimentos: {stats.get('movimentos', 0):,}")
    print(f"   Movimentos numerados: {stats.get('movimentos_numerados', 0):,}")
    print(f"   Itens de movimento: {stats.get('movimentos_itens', 0):,}")
    por_departamento = " | ".join(
        f"{nome} {stats.get('itens_departamento_' + nome.lower(), 0):,}"
        for nome in (DEPARTAMENTO_EQUIPAMENTO, DEPARTAMENTO_PECAS_ADI))
    print(f"   Itens por departamento: {por_departamento}")
    print(f"📄 Log: {log}")


if __name__ == "__main__":
    main()
