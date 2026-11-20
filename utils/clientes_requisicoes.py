"""Casamento dos clientes da planilha de requisições 2026.

A migração de clientes mescla secretarias/departamentos no cliente principal
(prefeitura), então comparar o nome da planilha com ``customers.name`` perde o
setor. O cadastro legado (``aluguel_legado.aluguel_clientes``) preserva o nome
exato usado na planilha, e cada endereço migrado guarda o
``legacy_customer_id`` e o ``alias`` do departamento de origem em
``addresses``. O caminho do casamento é::

    planilha --similaridade--> aluguel_clientes --de/para--> principal
             --addresses.legacy_customer_id--> customer + address

O de/para cobre as cópias mescladas na migração (``utils.depara_clientes``) e
``DEPARA_REQUISICOES_CLIENTES`` cobre os pseudo-clientes operacionais da
planilha (bases, estoques e rotas internas) que não existem no legado. Esse
de/para usa referências estáveis (id do legado ou nome exato do customer),
porque os ids do refatorado mudam quando a migração de clientes muda; os
destinos são resolvidos na carga e um alvo inexistente/ambíguo aborta a
importação antes de gravar qualquer requisição.
"""

import difflib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import text

from config.config import DEPARA_REQUISICOES_CLIENTES
from utils.depara_clientes import carregar_depara_clientes

PALAVRAS_IGNORADAS = {
    "DE", "DA", "DO", "DAS", "DOS", "E", "PARA", "P", "EM", "COM", "A", "O",
    "OS", "AS",
}

# Abaixo disso o casamento é considerado apenas uma sugestão. O de/para
# explícito de ``DEPARA_REQUISICOES_CLIENTES`` não passa por este limite.
LIMITE_PADRAO = 0.60
BONUS_CIDADE = 0.08
MARGEM_ENDERECO = 0.05


def normalizar(valor) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().upper()


def tokens_relevantes(texto: str) -> list[str]:
    return [t for t in normalizar(texto).split()
            if len(t) >= 2 and t not in PALAVRAS_IGNORADAS]


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _score(a: str, b: str) -> float:
    """Melhor entre a ordem original e os tokens ordenados.

    A planilha e o legado escrevem o mesmo nome em ordens diferentes
    (ex.: ``"INSUMOS - RENATO"`` x ``"RENATO - ROTA ACOPIARA"``), então a
    comparação por tokens ordenados evita perder o candidato certo.
    """
    return max(_ratio(a, b),
               _ratio(" ".join(sorted(a.split())), " ".join(sorted(b.split()))))


def mesma_cidade(cidade_planilha: str, cidade_legado: str) -> bool:
    """Cidade vazia no legado é desconhecida, não divergente."""
    if not cidade_legado:
        return True
    return (cidade_planilha == cidade_legado
            or cidade_planilha in cidade_legado
            or cidade_legado in cidade_planilha)


@dataclass
class ClienteRequisicao:
    """Destino de um cliente da planilha no banco refatorado."""

    legacy_id: int | None
    legacy_name: str
    city: str
    customer_id: int
    customer_name: str
    address_id: int | None
    address_alias: str
    score: float
    metodo: str

    def sugestao(self) -> dict:
        return {
            "legacy_id": self.legacy_id,
            "legacy_name": self.legacy_name,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "_similaridade": round(self.score, 4),
        }


class OverrideRequisicaoInvalido(ValueError):
    """Destino de ``DEPARA_REQUISICOES_CLIENTES`` inexistente ou ambíguo."""


class IndiceClientes:
    """Índice de ``aluguel_clientes`` com destino no refatorado."""

    def __init__(self, registros: list[dict], depara: dict[int, int] | None = None,
                 overrides: dict[str, object] | None = None,
                 catalogo: dict[int, dict] | None = None,
                 alias_endereco: dict[int, str] | None = None,
                 enderecos_por_customer: dict[int, list[dict]] | None = None):
        self.registros = registros
        self.depara = depara or {}
        # Catálogo de todos os customers, inclusive os que não têm endereço
        # com vínculo legado. Necessário para o de/para explícito apontar para
        # um customer sem ``legacy_customer_id`` (ex.: prefeitura sem endereço).
        self.catalogo = catalogo or {}
        self.alias_endereco = alias_endereco or {}
        self.enderecos_por_customer = enderecos_por_customer or {}
        self.overrides = {
            normalizar(nome): destino
            for nome, destino in (overrides or DEPARA_REQUISICOES_CLIENTES).items()
        }
        self.chaves: list[str] = []
        self.indice: dict[str, set[int]] = defaultdict(set)
        for posicao, registro in enumerate(registros):
            chave = normalizar(registro["legacy_name"])
            self.chaves.append(chave)
            for token in tokens_relevantes(chave):
                self.indice[token].add(posicao)
        self.por_customer: dict[int, dict] = {}
        self.por_legado: dict[int, dict] = {}
        for registro in registros:
            if registro.get("legacy_id") is not None:
                self.por_legado[int(registro["legacy_id"])] = registro
            if registro.get("customer_id") and registro.get("address_id"):
                self.por_customer.setdefault(int(registro["customer_id"]), registro)
        self.por_nome: dict[str, list[dict]] = defaultdict(list)
        for customer_id, dados in self.catalogo.items():
            self.por_nome[normalizar(dados.get("customer_name"))].append({
                "customer_id": int(customer_id),
                "customer_name": dados.get("customer_name", ""),
                "organization_id": dados.get("organization_id"),
            })
        # Resolve agora: um de/para inválido deve abortar antes de qualquer
        # gravação, e não virar FK quebrada no meio da importação.
        self.overrides_resolvidos = {
            alvo: self._resolver_override(alvo, destino)
            for alvo, destino in self.overrides.items()
        }

    # ------------------------------------------------------------------
    # Carga
    # ------------------------------------------------------------------
    @classmethod
    def carregar(cls, conn_legado, conn_novo, depara=None,
                 overrides=None) -> "IndiceClientes":
        legado = [dict(r) for r in conn_legado.execute(text("""
            SELECT id, nome_razao_social, cidade
            FROM aluguel_clientes
            WHERE deleted_at IS NULL
        """)).mappings()]
        enderecos = [dict(r) for r in conn_novo.execute(text("""
            SELECT a.id, a.addressable_id, a.alias, a.legacy_customer_id,
                   a.deleted_at, c.name
            FROM addresses a
            JOIN customers c ON c.id = a.addressable_id
            WHERE a.deleted_at IS NULL AND a.addressable_type = 'customer'
              AND a.legacy_customer_id IS NOT NULL
        """)).mappings()]

        # Cada id legado tem um endereço; quando houver repetição, prefere o
        # alias que preserva o departamento (diferente do nome do customer).
        mapa_endereco: dict[int, dict] = {}
        for endereco in enderecos:
            legado_id = int(endereco["legacy_customer_id"])
            atual = mapa_endereco.get(legado_id)
            if atual is None:
                mapa_endereco[legado_id] = endereco
                continue
            atual_generico = normalizar(atual["alias"]) == normalizar(atual["name"])
            novo_especifico = (normalizar(endereco["alias"])
                               != normalizar(endereco["name"]))
            if atual_generico and novo_especifico:
                mapa_endereco[legado_id] = endereco

        depara = carregar_depara_clientes() if depara is None else depara
        registros = []
        for cliente in legado:
            legado_id = int(cliente["id"])
            principal = _resolver_principal(legado_id, depara)
            endereco = mapa_endereco.get(principal) or mapa_endereco.get(legado_id)
            registros.append({
                "legacy_id": legado_id,
                "legacy_name": str(cliente["nome_razao_social"] or ""),
                "city": normalizar(cliente["cidade"]),
                "customer_id": int(endereco["addressable_id"]) if endereco else None,
                "customer_name": str(endereco["name"]) if endereco else "",
                "address_id": int(endereco["id"]) if endereco else None,
                "address_alias": str(endereco["alias"] or "") if endereco else "",
                "legacy_principal": principal,
            })
        catalogo, alias_endereco, enderecos_por_customer = \
            _carregar_catalogo_clientes(conn_novo)
        return cls(registros, depara=depara, overrides=overrides,
                   catalogo=catalogo, alias_endereco=alias_endereco,
                   enderecos_por_customer=enderecos_por_customer)

    # ------------------------------------------------------------------
    # Busca
    # ------------------------------------------------------------------
    def buscar(self, nome, cidade: str = "", limite: float = LIMITE_PADRAO,
               topo: int = 5
               ) -> tuple[ClienteRequisicao | None, float, list[dict]]:
        alvo = normalizar(nome)
        cidade_alvo = normalizar(cidade)
        if not alvo:
            return None, 0.0, []

        explicito = self._buscar_override(alvo, cidade_alvo)
        if explicito:
            return explicito, explicito.score, [explicito.sugestao()]

        candidatos: set[int] = set()
        for token in tokens_relevantes(alvo):
            candidatos |= self.indice.get(token, set())
        if len(candidatos) < 2:
            candidatos = set(range(len(self.registros)))

        pontuados: list[tuple[float, int]] = []
        for posicao in candidatos:
            chave = self.chaves[posicao]
            score = _score(alvo, chave)
            if cidade_alvo and cidade_alvo in chave.split():
                score = min(1.0, score + BONUS_CIDADE)
            pontuados.append((score, posicao))
        pontuados.sort(reverse=True)
        if not pontuados:
            return None, 0.0, []

        melhor_score, melhor_posicao = self._melhor_com_cidade(
            alvo, cidade_alvo, pontuados)
        sugestoes = [
            {
                "legacy_id": self.registros[p].get("legacy_id"),
                "legacy_name": self.registros[p].get("legacy_name", ""),
                "customer_id": self.registros[p].get("customer_id"),
                "customer_name": self.registros[p].get("customer_name", ""),
                "_similaridade": round(score, 4),
            }
            for score, p in pontuados[:topo]
        ]
        if melhor_score < limite:
            return None, melhor_score, sugestoes

        registro = self.registros[melhor_posicao]
        if not registro.get("customer_id") or not registro.get("address_id"):
            # O melhor nome pode ser uma cópia sem endereço migrado; aceita o
            # candidato com endereço mais próximo, dentro da margem.
            alternativo = next(
                ((score, p) for score, p in pontuados[:topo]
                 if self.registros[p].get("address_id")
                 and score >= melhor_score - MARGEM_ENDERECO), None)
            if not alternativo:
                return None, melhor_score, sugestoes
            melhor_score, melhor_posicao = alternativo
            registro = self.registros[melhor_posicao]

        return (self._registro_para_cliente(registro, melhor_score, "similaridade"),
                melhor_score, sugestoes)

    def _melhor_com_cidade(self, alvo: str, cidade_alvo: str,
                           pontuados: list[tuple[float, int]]
                           ) -> tuple[float, int]:
        melhor_score, melhor_posicao = pontuados[0]
        if not cidade_alvo:
            return melhor_score, melhor_posicao
        cidade_melhor = self.registros[melhor_posicao]["city"]
        if mesma_cidade(cidade_alvo, cidade_melhor):
            return melhor_score, melhor_posicao

        # O primeiro colocado é de outra cidade: restringe ao melhor candidato
        # da cidade da planilha antes de aceitar o global.
        da_cidade = [
            (score, p) for score, p in pontuados
            if mesma_cidade(cidade_alvo, self.registros[p]["city"])
            and self.registros[p]["city"]
        ]
        if da_cidade:
            score, posicao = da_cidade[0]
            if score >= LIMITE_PADRAO:
                return score, posicao
        return melhor_score, melhor_posicao

    def _registro_para_cliente(self, registro: dict, score: float,
                               metodo: str) -> ClienteRequisicao:
        return ClienteRequisicao(
            legacy_id=registro.get("legacy_id"),
            legacy_name=registro.get("legacy_name", ""),
            city=registro.get("city", ""),
            customer_id=int(registro["customer_id"]),
            customer_name=registro.get("customer_name", ""),
            address_id=(int(registro["address_id"])
                        if registro.get("address_id") else None),
            address_alias=registro.get("address_alias", ""),
            score=round(score, 4),
            metodo=metodo,
        )

    def _buscar_override(self, alvo: str, cidade_alvo: str
                         ) -> ClienteRequisicao | None:
        return self.overrides_resolvidos.get(alvo)

    # ------------------------------------------------------------------
    # De/para explícito (DEPARA_REQUISICOES_CLIENTES)
    # ------------------------------------------------------------------
    def _resolver_override(self, alvo: str, destino: object
                           ) -> ClienteRequisicao:
        if isinstance(destino, int):
            # Compatibilidade: id do customer. Instável entre execuções; o
            # catálogo é validado para falhar cedo se o id não existir.
            return self._override_por_customer_id(
                alvo, {"customer_id": destino})
        if not isinstance(destino, dict):
            raise OverrideRequisicaoInvalido(
                f"{alvo!r}: destino inválido em DEPARA_REQUISICOES_CLIENTES "
                f"({destino!r}); use {{'legado': ...}} ou {{'cliente': ...}}")
        if "legado" in destino:
            return self._override_por_legado(alvo, destino)
        if "cliente" in destino:
            return self._override_por_nome(alvo, destino)
        if "customer_id" in destino:
            return self._override_por_customer_id(alvo, destino)
        raise OverrideRequisicaoInvalido(
            f"{alvo!r}: destino sem 'legado', 'cliente' ou 'customer_id' "
            f"em DEPARA_REQUISICOES_CLIENTES ({destino!r})")

    def _override_por_legado(self, alvo: str,
                             destino: dict) -> ClienteRequisicao:
        legado_id = int(destino["legado"])
        registro = self.por_legado.get(legado_id)
        if registro is None:
            raise OverrideRequisicaoInvalido(
                f"{alvo!r}: aluguel_clientes.id={legado_id} não existe no "
                f"legado (ou está excluído); confira DEPARA_REQUISICOES_CLIENTES")
        if not registro.get("customer_id") or not registro.get("address_id"):
            raise OverrideRequisicaoInvalido(
                f"{alvo!r}: cliente legado {legado_id} "
                f"({registro.get('legacy_name')!r}) não tem customer/endereço "
                f"migrado (customer={registro.get('customer_id')}, "
                f"address={registro.get('address_id')}); use {{'cliente': ..., "
                f"'sem_endereco': True}} se for intencional")
        return self._cliente_override(
            customer_id=int(registro["customer_id"]),
            customer_name=registro.get("customer_name", ""),
            address_id=int(registro["address_id"]),
            address_alias=registro.get("address_alias", ""),
            legacy_id=registro.get("legacy_id"),
            legacy_name=(destino.get("observacao")
                         or registro.get("legacy_name", "")),
            city=registro.get("city", ""),
        )

    def _override_por_nome(self, alvo: str,
                           destino: dict) -> ClienteRequisicao:
        nome = destino["cliente"]
        org = destino.get("org", destino.get("organization_id"))
        candidatos = [
            c for c in self.por_nome.get(normalizar(nome), [])
            if org is None or c.get("organization_id") == int(org)
        ]
        if not candidatos:
            sufixo = f" na organização {org}" if org is not None else ""
            raise OverrideRequisicaoInvalido(
                f"{alvo!r}: customer {nome!r}{sufixo} não encontrado; "
                f"confira DEPARA_REQUISICOES_CLIENTES")
        if len(candidatos) > 1:
            ids = [c["customer_id"] for c in candidatos]
            raise OverrideRequisicaoInvalido(
                f"{alvo!r}: customer {nome!r} ambíguo ({len(candidatos)} "
                f"cadastros: {ids}); informe 'org' ou use {{'legado': ...}}")
        customer = candidatos[0]
        customer_id = int(customer["customer_id"])
        enderecos = self.enderecos_por_customer.get(customer_id, [])
        if destino.get("sem_endereco"):
            endereco = None
        elif "endereco" in destino:
            alvo_endereco = normalizar(destino["endereco"])
            achados = [e for e in enderecos
                       if normalizar(e.get("alias")) == alvo_endereco]
            if len(achados) != 1:
                disponiveis = [e.get("alias") for e in enderecos]
                raise OverrideRequisicaoInvalido(
                    f"{alvo!r}: endereço {destino['endereco']!r} encontrado "
                    f"{len(achados)}x em {nome!r} (customer {customer_id}); "
                    f"endereços: {disponiveis}")
            endereco = achados[0]
        else:
            endereco = enderecos[0] if enderecos else None
        return self._cliente_override(
            customer_id=customer_id,
            customer_name=customer.get("customer_name", ""),
            address_id=int(endereco["id"]) if endereco else None,
            address_alias=(endereco.get("alias") or "") if endereco else "",
            legacy_id=None,
            legacy_name=(destino.get("observacao")
                         or customer.get("customer_name", "")),
            city="",
        )

    def _override_por_customer_id(self, alvo: str,
                                  destino: dict) -> ClienteRequisicao:
        """Caminho antigo por id do customer (instável entre execuções)."""
        customer_id = int(destino["customer_id"])
        if customer_id not in self.catalogo:
            raise OverrideRequisicaoInvalido(
                f"{alvo!r}: customer {customer_id} não existe no refatorado; "
                f"prefira {{'legado': ...}} em DEPARA_REQUISICOES_CLIENTES")
        # ``address_id`` ausente significa "usa o endereço padrão do customer";
        # ``None`` explícito significa "sem endereço".
        address_id = destino.get("address_id", "auto")
        registro = self.por_customer.get(customer_id, {})
        catalogo = self.catalogo.get(customer_id, {})
        if address_id == "auto":
            address_id = registro.get("address_id") or catalogo.get("address_id")
        if address_id is not None and int(address_id) not in self.alias_endereco:
            raise OverrideRequisicaoInvalido(
                f"{alvo!r}: endereço {address_id} não pertence a customer "
                f"ativo; prefira {{'legado': ...}} em "
                f"DEPARA_REQUISICOES_CLIENTES")
        alias = (self.alias_endereco.get(int(address_id), "")
                 if address_id else "")
        return self._cliente_override(
            customer_id=customer_id,
            customer_name=(registro.get("customer_name")
                           or catalogo.get("customer_name", "")),
            address_id=int(address_id) if address_id else None,
            address_alias=alias,
            legacy_id=registro.get("legacy_id"),
            legacy_name=(destino.get("observacao")
                         or registro.get("legacy_name", "")
                         or catalogo.get("customer_name", "")),
            city=registro.get("city", ""),
        )

    def _cliente_override(self, *, customer_id: int,
                          customer_name: str, address_id: int | None,
                          address_alias: str, legacy_id: int | None,
                          legacy_name: str, city: str) -> ClienteRequisicao:
        return ClienteRequisicao(
            legacy_id=legacy_id,
            legacy_name=legacy_name or customer_name,
            city=city,
            customer_id=customer_id,
            customer_name=customer_name,
            address_id=address_id,
            address_alias=address_alias,
            score=1.0,
            metodo="explicito",
        )


def _carregar_catalogo_clientes(
        conn) -> tuple[dict[int, dict], dict[int, str], dict[int, list[dict]]]:
    """Customers ativos, endereço padrão (menor id) e alias por endereço."""
    linhas = conn.execute(text("""
        SELECT c.id AS customer_id, c.name, c.organization_id,
               a.id AS address_id, a.alias
        FROM customers c
        LEFT JOIN addresses a ON a.addressable_id = c.id
             AND a.addressable_type = 'customer' AND a.deleted_at IS NULL
        WHERE c.deleted_at IS NULL
        ORDER BY c.id, a.id
    """)).mappings()
    catalogo: dict[int, dict] = {}
    alias_endereco: dict[int, str] = {}
    enderecos_por_customer: dict[int, list[dict]] = defaultdict(list)
    for linha in linhas:
        customer_id = int(linha["customer_id"])
        if linha["address_id"]:
            alias_endereco[int(linha["address_id"])] = str(linha["alias"] or "")
            enderecos_por_customer[customer_id].append({
                "id": int(linha["address_id"]),
                "alias": str(linha["alias"] or ""),
            })
        atual = catalogo.get(customer_id)
        if atual is None:
            catalogo[customer_id] = {
                "customer_name": str(linha["name"] or ""),
                "organization_id": int(linha["organization_id"]),
                "address_id": int(linha["address_id"]) if linha["address_id"] else None,
                "address_alias": str(linha["alias"] or ""),
            }
    return catalogo, alias_endereco, enderecos_por_customer


def _resolver_principal(legado_id: int, depara: dict[int, int]) -> int:
    vistos: set[int] = set()
    principal = legado_id
    while principal in depara and principal not in vistos:
        vistos.add(principal)
        principal = int(depara[principal])
    return principal
