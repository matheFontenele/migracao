"""Deriva a cidade de um endereço migrado a partir da prefeitura de origem.

O legado grava ``CIDADE = 'NÃO INFORMADO'`` nos endereços das secretarias: a
cidade existe só na raiz (prefeitura), que muitas vezes não tem endereço
próprio. Sem isso, a tela de contratos não oferece a cidade no filtro, porque
nenhum endereço de cliente com contrato aponta para ela.

A derivação é conservadora, no mesmo espírito de ``endereco_uf``: o nome da
prefeitura é reduzido ao município (removendo prefixos como
"PREFEITURA MUNICIPAL DE", sufixos de UF, anos e observações) e só é aceito
quando o resultado aponta para **um único** município da base do IBGE.
Homônimos (Cascavel/CE x Cascavel/PR), órgãos que não são município
("MINISTÉRIO DO TRABALHO E EMPREGO") e nomes com lixo continuam sem cidade em
vez de receberem um chute.
"""
import re

from cadastros.clientes.endereco_uf import uf_do_municipio

# Valores que o pipeline/legado usa quando a cidade não veio preenchida.
PLACEHOLDERS_CIDADE = frozenset({
    "",
    "0",
    "-",
    "NAN",
    "NONE",
    "NÃO INFORMADO",
    "NAO INFORMADO",
    "SEM INFORMAÇÃO",
    "SEM INFORMACAO",
})

# Do mais específico para o mais genérico: o primeiro que casar é removido.
_PREFIXOS = (
    r"PREFEITURA\s+MUNICIPAL\s+(?:DE|DO|DA|DAS|DOS)",
    r"PREFEITURA\s+(?:DE|DO|DA|DAS|DOS)",
    r"MUNIC[IÍ]PIO\s+(?:DE|DO|DA|DAS|DOS)",
    r"PREFEITURA\s+MUNICIPAL",
    r"PREFEITURA\s+M\s+(?:DE|DO|DA)",
    r"P\.?\s*M\.?\s+(?:DE|DO|DA)",
    r"PREFEITURA",
    r"MUNIC[IÍ]PIO",
)
_PREFIXO_RE = re.compile(r"^\s*(?:%s)\s+" % "|".join(_PREFIXOS), re.IGNORECASE)
_SUFIXO_UF_RE = re.compile(r"\s*[/-]\s*[A-Za-z]{2}\s*$")
_ANO_RE = re.compile(r"\s+\d{4}\s*$")
_TRECHO_RE = re.compile(r"\s+-\s+.*$")
_PARENTESES_RE = re.compile(r"\([^)]*\)")


def _candidato(prefeitura):
    """Trecho do nome da prefeitura que deveria ser o município."""
    texto = "" if prefeitura is None else str(prefeitura).strip()
    if not texto or texto.lower() == "nan":
        return None
    texto = _SUFIXO_UF_RE.sub("", texto)
    texto = _TRECHO_RE.sub("", texto)
    texto = _PARENTESES_RE.sub(" ", texto)
    texto = _PREFIXO_RE.sub("", texto)
    texto = _ANO_RE.sub("", texto)
    texto = texto.strip(" -,.")
    return texto or None


def municipio_da_prefeitura(prefeitura):
    """Município (grafia do legado, em caixa alta) da prefeitura, ou ``None``."""
    candidato = _candidato(prefeitura)
    if not candidato:
        return None
    if not uf_do_municipio(candidato):
        return None
    return candidato.upper()


def resolver_cidade(cidade, prefeitura):
    """Cidade a gravar no endereço, ou ``None`` quando nada deve mudar.

    Só atua quando a cidade atual é placeholder; endereços já preenchidos e
    prefeituras sem município único permanecem como estão.
    """
    atual = "" if cidade is None else str(cidade).strip()
    if atual.upper() not in PLACEHOLDERS_CIDADE:
        return None
    return municipio_da_prefeitura(prefeitura)
