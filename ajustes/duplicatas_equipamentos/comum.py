#!/usr/bin/env python3
"""Utilidades compartilhadas do ajuste de duplicatas de equipamentos.

Concentra caminhos, leitura/escrita dos CSVs (convencao de `ajustes/`: `;` e
UTF-8 com BOM), leitura dos Parquets, a regra deterministica do canonico e a
avaliacao das travas.
"""

from __future__ import annotations

import csv
import hashlib
import sys
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).resolve().parent
RAIZ = PASTA.parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

OUT = PASTA / "out"
MAPA_PADRAO = PASTA / "mapa_canonico.csv"
PASTA_PARQUETS = RAIZ / "docs" / "parquets"

REASON_ENTRADA = "TRANSACTION_ENTRANCE_EQUIPMENT"

ESCOPO_ORGAO = "orgao"
ESCOPO_GLOBAL = "global"
REGRA_ATUAL = "atual"
REGRA_HISTORICO = "historico"

STATUS_NOMES = {
    1: "DISPONIVEL",
    2: "ALUGADO",
    3: "RESERVADO",
    4: "EMPRESTADO",
    5: "USO INTERNO",
    6: "EM MANUTENCAO",
    7: "NA GARANTIA",
    8: "EM DEVOLUCAO",
    9: "BAIXADO",
}

# Ordem de preferencia do status na regra `atual` (menor rank vence).
STATUS_ORDEM = {2: 0, 1: 1, 4: 2, 6: 3, 3: 4, 5: 5, 7: 6, 8: 7}

# (tabela, coluna, tem deleted_at)
TABELAS_REF = (
    ("equipment_history", "equipment_id", True),
    ("movement_items", "equipment_id", True),
    ("service_order_items", "equipment_id", True),
    ("maintenances", "equipment_id", True),
    ("return_confirmations_items", "equipment_id", True),
    ("volume_items", "equipment_id", False),
    ("equipment_upgrades", "equipment_id", True),
    ("transaction_items", "equipment_id", True),
)

MOTIVOS_TRADUCAO = {
    "PI_DIVERGENTE": "product_item_id divergente dentro do grupo (produto diferente)",
    "PARQUET_DUPLO": "2+ ids do grupo citados nos Parquets (duas maquinas alugadas)",
    "COLISAO_MOVIMENTO": "duas copias no mesmo movimento + operacao",
    "RESERVADO": "equipamento com reserved_id preenchido",
    "OS_ABERTA": "item em ordem de servico aberta",
    "MANUTENCAO_ABERTA": "manutencao aberta",
    "MAIS_DE_DUAS_COPIAS": "grupo com mais de 2 copias",
    "CANONICO_INVALIDO": "canonico calculado esta descartado/baixado",
}

CABECALHO_CANDIDATOS = [
    "escopo", "id", "number", "name", "status_id", "status_nome",
    "current_organization_id", "product_item_id", "transaction_item_id",
    "serial_number", "reserved_id", "address_id", "location_id",
    "created_at", "deleted_at",
    "qtd_movimentos", "ultimo_movimento", "cliente_ultimo", "operacao_ultima",
    "refs_equipment_history", "refs_equipment_history_entrada",
    "refs_movement_items", "refs_service_order_items", "refs_maintenances",
    "refs_return_confirmations_items", "refs_volume_items",
    "refs_equipment_upgrades", "refs_related_equipments",
    "refs_transaction_items", "refs_total_repointar",
    "tem_parquet", "arquivo_parquet", "priorizado",
    "grupo_repeticoes", "pi_divergente_no_grupo", "name_divergente_no_grupo",
    "status_divergente_no_grupo", "tem_algum_alugado",
    "qtd_ids_parquet_no_grupo", "colisao_mesmo_movimento",
    "em_os_aberta", "em_manutencao_aberta", "reservado_no_grupo",
]

CABECALHO_MAPA = [
    "grupo_id", "escopo", "tombo", "orgao_id", "equipment_id", "canonical_id",
    "acao", "regra_decisora", "status_id", "tem_parquet", "qtd_movimentos",
    "ultimo_movimento", "product_item_id", "transaction_item_id", "name",
    "revisado_por", "aprovado", "motivo", "exige_revisao",
    "justificativa", "canonical_manual",
]


def agora_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def formatar_data(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d")
    texto = str(valor).strip()
    return texto[:10] if len(texto) >= 10 else texto


def data_ordenavel(valor) -> str:
    """String ordenavel para datas (vazio = sem movimento, ordena primeiro)."""
    texto = str(valor or "").strip()
    return texto if texto else "0000-00-00"


def normalizar_int(valor):
    if valor is None:
        return None
    if isinstance(valor, bool):
        return int(valor)
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float):
        return int(valor)
    texto = str(valor).strip()
    if not texto or texto.upper() in {"NULL", "NONE", "NAN"}:
        return None
    try:
        return int(float(texto))
    except (TypeError, ValueError):
        return None


def normalizar_float(valor):
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if not texto or texto.upper() in {"NULL", "NONE", "NAN"}:
        return None
    try:
        return float(texto)
    except (TypeError, ValueError):
        return None


def sim(valor) -> bool:
    return str(valor or "").strip().upper() in {"S", "1", "TRUE", "SIM"}


def escrever_csv(caminho, colunas, linhas) -> Path:
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.DictWriter(
            arquivo, fieldnames=colunas, delimiter=";", extrasaction="ignore"
        )
        escritor.writeheader()
        for linha in linhas:
            escritor.writerow(linha)
    return caminho


def ler_csv(caminho) -> list[dict]:
    caminho = Path(caminho)
    with caminho.open(newline="", encoding="utf-8-sig") as arquivo:
        return [dict(linha) for linha in csv.DictReader(arquivo, delimiter=";")]


def sha256_arquivo(caminho) -> str:
    digest = hashlib.sha256()
    with Path(caminho).open("rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1 << 20), b""):
            digest.update(bloco)
    return digest.hexdigest()


def carregar_indice_parquet(pasta=PASTA_PARQUETS, log=print) -> dict[int, list[str]]:
    """{equipment_id: [arquivos parquet que o citam]}.

    Le apenas a coluna EQUIPAMENTO_ID. O id do refatorado e o id do legado
    preservado pela migracao (`docs/plano_ajuste_duplicatas_refatorado.md` §8).
    """
    import pandas as pd

    indice: dict[int, list[str]] = {}
    arquivos = sorted(Path(pasta).glob("*.parquet"))
    if not arquivos:
        log(f"AVISO: nenhum Parquet em {pasta}; sinal tem_parquet fica vazio.")
        return indice

    falhas = 0
    for arquivo in arquivos:
        try:
            df = pd.read_parquet(arquivo, columns=["EQUIPAMENTO_ID"])
        except Exception:
            try:
                df = pd.read_parquet(arquivo)
                df.columns = [str(c).upper() for c in df.columns]
                if "EQUIPAMENTO_ID" not in df.columns:
                    continue
                df = df[["EQUIPAMENTO_ID"]]
            except Exception:
                falhas += 1
                continue
        ids = pd.to_numeric(df["EQUIPAMENTO_ID"], errors="coerce").dropna()
        for valor in {int(v) for v in ids}:
            indice.setdefault(valor, []).append(arquivo.name)

    if falhas:
        log(f"AVISO: {falhas} Parquet(s) ilegiveis; sinal tem_parquet parcial.")
    return indice


def carregar_priorizados() -> set[int]:
    from config.parquet_excecoes import ids_priorizados

    return {int(i) for i in ids_priorizados()}


def avaliar_travas(linhas: list[dict]) -> list[str]:
    """Travas do plano §6, a partir dos sinais de grupo repetidos nas linhas."""
    def algum(campo: str) -> bool:
        return any(sim(linha.get(campo)) for linha in linhas)

    motivos: list[str] = []
    if algum("pi_divergente_no_grupo"):
        motivos.append("PI_DIVERGENTE")
    if any((normalizar_int(l.get("qtd_ids_parquet_no_grupo")) or 0) >= 2 for l in linhas):
        motivos.append("PARQUET_DUPLO")
    if algum("colisao_mesmo_movimento"):
        motivos.append("COLISAO_MOVIMENTO")
    if algum("reservado_no_grupo"):
        motivos.append("RESERVADO")
    if algum("em_os_aberta"):
        motivos.append("OS_ABERTA")
    if algum("em_manutencao_aberta"):
        motivos.append("MANUTENCAO_ABERTA")
    if len(linhas) > 2:
        motivos.append("MAIS_DE_DUAS_COPIAS")
    return motivos


def _chave_status(linha: dict) -> int:
    return STATUS_ORDEM.get(normalizar_int(linha.get("status_id")), 99)


def _chave_priorizado(linha: dict) -> int:
    return 1 if sim(linha.get("priorizado")) else 0


def _chave_parquet(linha: dict) -> int:
    return 1 if sim(linha.get("tem_parquet")) else 0


def _chave_qtd_movimentos(linha: dict) -> int:
    return normalizar_int(linha.get("qtd_movimentos")) or 0


def _chave_ultimo_movimento(linha: dict) -> str:
    return data_ordenavel(linha.get("ultimo_movimento"))


def _chave_menor_id(linha: dict) -> int:
    return normalizar_int(linha.get("id")) or 0


def escolher_canonico(linhas: list[dict], regra: str = REGRA_ATUAL):
    """Aplica a regra do plano §6 em ordem; devolve (linha, criterio_decisor)."""
    if regra == REGRA_HISTORICO:
        criterios = [
            ("priorizado", _chave_priorizado, True),
            ("parquet", _chave_parquet, True),
            ("qtd_movimentos", _chave_qtd_movimentos, True),
            ("ultimo_movimento", _chave_ultimo_movimento, True),
            ("menor_id", _chave_menor_id, False),
        ]
    else:
        criterios = [
            ("priorizado", _chave_priorizado, True),
            ("parquet", _chave_parquet, True),
            ("status", _chave_status, False),
            ("ultimo_movimento", _chave_ultimo_movimento, True),
            ("qtd_movimentos", _chave_qtd_movimentos, True),
            ("menor_id", _chave_menor_id, False),
        ]

    restantes = list(linhas)
    decisor = "empate"
    for nome, funcao, manter_maior in criterios:
        valores = [funcao(linha) for linha in restantes]
        if len(set(valores)) <= 1:
            continue
        alvo = max(valores) if manter_maior else min(valores)
        restantes = [l for l in restantes if funcao(l) == alvo]
        decisor = nome
        if len(restantes) == 1:
            break

    if len(restantes) > 1:
        restantes.sort(key=_chave_menor_id)
    return restantes[0], decisor
