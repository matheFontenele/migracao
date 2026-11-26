"""Resolve a UF de um endereço a partir do município.

O legado guarda o estado como `'0'` quando não conhece a UF e, em muitos
casos, o município está preenchido mesmo assim. A base município -> UF fica em
``config/municipios_uf.json``, gerada a partir da API do IBGE
(``https://servicodados.ibge.gov.br/api/v1/localidades/municipios``).

Regra deliberadamente conservadora: só resolve quando o nome normalizado
aponta para **uma única** UF. Homônimos (Cascavel/CE x Cascavel/PR,
Iguaçu/CE x Iguaçu/PR etc.) e nomes fora da base — distritos como Pecém,
grafias erradas como "Fortaelza" — continuam sem UF em vez de receberem um
chute. Quem chama deve tratar ``None`` como "não informado".
"""
import json
import os

from utils.sanetizador import normalizar_para_match

ARQUIVO_MUNICIPIOS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "municipios_uf.json",
)

UFS_VALIDAS = frozenset({
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
    "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
    "SE", "SP", "TO",
})

SEM_UF = "NÃO INFORMADO"

_municipios = None


def _carregar_municipios():
    global _municipios
    if _municipios is None:
        with open(ARQUIVO_MUNICIPIOS, encoding="utf-8") as arquivo:
            _municipios = json.load(arquivo)
    return _municipios


def uf_do_municipio(cidade):
    """UF do município, ou ``None`` se não houver resolução única.

    ``None`` cobre três casos que não devem ser adivinhados: município vazio,
    município homônimo (mais de uma UF) e município fora da base do IBGE.
    """
    chave = normalizar_para_match(cidade)
    if not chave:
        return None
    ufs = _carregar_municipios().get(chave)
    if not ufs or len(ufs) != 1:
        return None
    return ufs[0]


def normalizar_uf(estado, cidade=None):
    """UF válida para gravar em ``addresses.state``.

    Mantém a UF quando o valor já é válido (comparação sem caixa), deriva do
    município quando o valor é ausente/placeholder (`'0'`, `'NÃO INFORMADO'`,
    `''`) e, se não houver como resolver, devolve ``SEM_UF``.
    """
    uf = str(estado).strip().upper() if estado is not None else ""
    if uf in UFS_VALIDAS:
        return uf
    return uf_do_municipio(cidade) or SEM_UF
