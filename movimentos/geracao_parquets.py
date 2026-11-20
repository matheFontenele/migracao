"""Validação e publicação dos vínculos equipamento→contrato gerados externamente."""
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text


PROJETO = Path(os.getenv("PARQUETS_PROJETO", "/home/matheus/Equipaments_for_contracts"))
CONTAINER = os.getenv("PARQUETS_CONTAINER", "streamlit_automacao_contratos")
ORIGEM = Path(os.getenv("PARQUETS_ORIGEM", str(PROJETO / "locks_parquet")))
DESTINO = Path(os.getenv("PARQUETS_DESTINO", "./docs/parquets"))
GERAR = os.getenv("PARQUETS_GERAR", "1")
COLUNAS_OBRIGATORIAS = {"EQUIPAMENTO_ID", "TOMBO", "CLIENTE_ID", "CONTRACT_ID"}


def _validar_arquivos(origem, contratos_validos, itens_validos):
    arquivos = sorted(Path(origem).glob("*.parquet"))
    if not arquivos:
        raise ValueError(f"Nenhum arquivo .parquet encontrado em {origem}")

    ids_equipamentos = set()
    contratos = set()
    itens = set()
    linhas = 0
    organizacoes = set()
    for arquivo in arquivos:
        df = pd.read_parquet(arquivo)
        faltantes = COLUNAS_OBRIGATORIAS - set(df.columns)
        if faltantes:
            raise ValueError(f"{arquivo.name}: colunas obrigatórias ausentes: {sorted(faltantes)}")
        if df["EQUIPAMENTO_ID"].duplicated().any():
            duplicados = df.loc[df["EQUIPAMENTO_ID"].duplicated(keep=False), "EQUIPAMENTO_ID"].tolist()
            raise ValueError(f"EQUIPAMENTO_ID duplicado em {arquivo.name}: {duplicados[:20]}")
        ids = set(df["EQUIPAMENTO_ID"].dropna().tolist())
        repetidos = ids_equipamentos.intersection(ids)
        if repetidos:
            raise ValueError(f"EQUIPAMENTO_ID duplicado entre arquivos: {list(repetidos)[:20]}")
        ids_equipamentos.update(ids)
        if df["CONTRACT_ID"].isna().any():
            raise ValueError(f"CONTRACT_ID nulo em {arquivo.name}")
        contratos.update(df["CONTRACT_ID"].tolist())
        if "CONTRACT_ITEM_ID" in df.columns:
            itens.update(df["CONTRACT_ITEM_ID"].dropna().tolist())
        if "ORGAO_ID" in df.columns:
            organizacoes.update(df["ORGAO_ID"].dropna().tolist())
        linhas += len(df)

    orfaos_contratos = contratos - set(contratos_validos)
    if orfaos_contratos:
        raise ValueError(f"CONTRACT_ID inexistente em contracts: {list(orfaos_contratos)[:20]}")
    orfaos_itens = itens - set(itens_validos)
    if orfaos_itens:
        raise ValueError(f"CONTRACT_ITEM_ID inexistente em contract_items: {list(orfaos_itens)[:20]}")
    return {"arquivos": arquivos, "linhas": linhas, "contratos": contratos,
            "organizacoes": organizacoes, "equipamentos": len(ids_equipamentos)}


def _publicar(origem, destino):
    origem, destino = Path(origem), Path(destino)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    staging = destino / f"_staging_{timestamp}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        arquivos = sorted(origem.glob("*.parquet"))
        if not arquivos:
            raise ValueError(f"Nenhum arquivo .parquet encontrado em {origem}")
        for arquivo in arquivos:
            shutil.copy2(arquivo, staging / arquivo.name)
        anteriores = sorted(destino.glob("*.parquet"))
        if anteriores:
            pasta_anterior = destino / "_anterior" / timestamp
            pasta_anterior.mkdir(parents=True, exist_ok=True)
            for arquivo in anteriores:
                shutil.move(str(arquivo), str(pasta_anterior / arquivo.name))
        publicados = []
        for arquivo in sorted(staging.glob("*.parquet")):
            alvo = destino / arquivo.name
            if alvo.exists():
                raise FileExistsError(f"Arquivo de destino já existe: {alvo}")
            shutil.move(str(arquivo), str(alvo))
            publicados.append(alvo)
        return publicados
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _buscar_ids(conn, tabela, ids):
    validos = set()
    ids = list(ids)
    for inicio in range(0, len(ids), 1000):
        lote = ids[inicio:inicio + 1000]
        if not lote:
            continue
        placeholders = ", ".join(f":id_{i}" for i in range(len(lote)))
        parametros = {f"id_{i}": valor for i, valor in enumerate(lote)}
        rows = conn.execute(text(f"SELECT id FROM {tabela} WHERE id IN ({placeholders})"), parametros)
        validos.update(row[0] for row in rows)
    return validos


def executar(eng_novo):
    if GERAR == "1":
        comando = ["docker", "exec", "-w", "/app", CONTAINER, "python", "batch_parquets.py",
                   "--limpar", "--resumo-json", "docs/batch_resumo.json"]
        try:
            resultado = subprocess.run(comando, capture_output=True, text=True, timeout=3600)
        except (FileNotFoundError, subprocess.TimeoutExpired) as erro:
            raise RuntimeError(f"Falha ao executar gerador de Parquets: {erro}") from erro
        if resultado.returncode:
            raise RuntimeError("Gerador de Parquets falhou.\nSTDOUT:\n" + "\n".join(resultado.stdout.splitlines()[-40:]) +
                               "\nSTDERR:\n" + "\n".join(resultado.stderr.splitlines()[-40:]))

    resumo = PROJETO / "docs" / "batch_resumo.json"
    if resumo.exists():
        dados = json.loads(resumo.read_text(encoding="utf-8"))
        totais = dados.get("totais", {})
        print(f"Resumo do batch ({dados.get('gerado_em', 'sem data')}):")
        for organizacao, numeros in dados.get("orgs", {}).items():
            print(f"  {organizacao}: {numeros.get('vinculados', 0)} equipamento(s) vinculado(s), "
                  f"{numeros.get('sem_contrato', 0)} sem contrato, {numeros.get('arquivos', 0)} arquivo(s)")
        print(f"  TOTAL: {totais.get('vinculados', 0)} vinculados / "
              f"{totais.get('equipamentos', 0)} equipamentos")
    else:
        print(f"⚠️ Resumo do batch não encontrado: {resumo}")
    pendentes = PROJETO / "docs" / "pendentes_vinculacao.csv"
    if pendentes.exists():
        quantidade = max(0, sum(1 for _ in pendentes.open(encoding="utf-8")) - 1)
        print(f"⚠️ {quantidade} vínculos pendentes em {pendentes}")

    arquivos = sorted(ORIGEM.glob("*.parquet"))
    if not arquivos:
        raise ValueError(f"Nenhum arquivo .parquet encontrado em {ORIGEM}")
    contratos, itens = set(), set()
    for arquivo in arquivos:
        df = pd.read_parquet(arquivo)
        contratos.update(df["CONTRACT_ID"].dropna().tolist())
        if "CONTRACT_ITEM_ID" in df:
            itens.update(df["CONTRACT_ITEM_ID"].dropna().tolist())
    with eng_novo.connect() as conn:
        contratos_validos = _buscar_ids(conn, "contracts", contratos)
        itens_validos = _buscar_ids(conn, "contract_items", itens)
    stats = _validar_arquivos(ORIGEM, contratos_validos, itens_validos)
    publicados = _publicar(ORIGEM, DESTINO)
    stats["arquivos_publicados"] = len(publicados)
    print(f"✅ Publicados {len(publicados)} arquivos, {stats['linhas']} linhas, "
          f"{len(stats['contratos'])} contratos e {len(stats['organizacoes'])} organizações.")
    return stats
