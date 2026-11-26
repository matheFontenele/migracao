"""Normaliza a UF dos endereços migrados.

O legado grava `estado = '0'` quando não conhece a UF. Este ajuste roda depois
da carga de clientes e:

1. deriva a UF do município quando a resolução é única (base do IBGE em
   ``config/municipios_uf.json``), e
2. troca os placeholders inválidos (`'0'`, `''`, `'NÃO INFORMADO'`, homônimos,
   municípios fora da base) por `'NÃO INFORMADO'`, que é o valor que o restante
   da migração já usa.

Nada de município é alterado: se a cidade também não veio do legado, o
endereço continua sem cidade e sem UF — não há de onde recuperar.

Uso:
    python -m ajustes.corrigir_destinos_endereco                # dry-run
    python -m ajustes.corrigir_destinos_endereco --apply
"""
import argparse
import csv
import os
from collections import Counter
from datetime import datetime

from sqlalchemy import create_engine, text

from cadastros.clientes.endereco_uf import SEM_UF, normalizar_uf

URL_PADRAO = (
    "mysql+pymysql://root:root@127.0.0.1:3307/"
    "controle-interno?charset=utf8mb4"
)
PASTA_AJUSTES = os.path.dirname(os.path.abspath(__file__))


def _texto(valor):
    return "" if valor is None else str(valor).strip()


def levantar(conn):
    return conn.execute(text("""
        SELECT a.id, a.city, a.state, a.alias, a.legacy_customer_id,
               cu.name AS cliente
        FROM addresses a
        LEFT JOIN customers cu
               ON cu.id = a.addressable_id AND a.addressable_type = 'customer'
        WHERE a.deleted_at IS NULL
        ORDER BY a.id
    """)).all()


def planejar(linhas):
    """Devolve (mudancas, contagens) sem tocar no banco."""
    mudancas = []
    contagem = Counter()
    for linha in linhas:
        atual = _texto(linha.state).upper()
        nova = normalizar_uf(linha.state, linha.city)
        if nova == atual:
            continue
        origem = "derivada_do_municipio" if nova != SEM_UF else "sem_uf"
        contagem[origem] += 1
        contagem[f"de:{atual or '<vazio>'}"] += 1
        mudancas.append({
            "address_id": linha.id,
            "cliente": linha.cliente,
            "alias": linha.alias,
            "city": linha.city,
            "uf_antes": atual,
            "uf_depois": nova,
            "origem": origem,
        })
    return mudancas, contagem


def aplicar(conn, mudancas):
    conn.execute(
        text("UPDATE addresses SET state = :uf, updated_at = NOW() WHERE id = :id"),
        [{"uf": m["uf_depois"], "id": m["address_id"]} for m in mudancas],
    )


def gravar_relatorio(mudancas, carimbo):
    caminho = os.path.join(PASTA_AJUSTES, f"destinos_uf_corrigidos_{carimbo}.csv")
    with open(caminho, "w", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=list(mudancas[0].keys()))
        escritor.writeheader()
        escritor.writerows(mudancas)
    return caminho


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=URL_PADRAO)
    parser.add_argument("--apply", action="store_true",
                        help="Grava as alterações (padrão: dry-run).")
    args = parser.parse_args()
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    engine = create_engine(args.url)

    with engine.begin() as conn:
        linhas = levantar(conn)
        mudancas, contagem = planejar(linhas)

        print(f"🔎 endereços ativos analisados: {len(linhas)}")
        if not mudancas:
            print("✅ nada a corrigir: todas as UFs já estão válidas.")
            return 0

        print(f"🛠️  endereços a corrigir: {len(mudancas)}")
        print(f"   UF derivada do município: {contagem['derivada_do_municipio']}")
        print(f"   normalizados p/ '{SEM_UF}': {contagem['sem_uf']}")
        print("   valores de origem:")
        for chave, valor in sorted(contagem.items()):
            if chave.startswith("de:"):
                print(f"      {chave[3:]:>14} -> {valor}")

        por_uf = Counter(m["uf_depois"] for m in mudancas)
        print("   destino das UFs:", dict(sorted(por_uf.items())))

        sem_resolucao = Counter(
            _texto(m["city"]) or "<sem cidade>"
            for m in mudancas if m["origem"] == "sem_uf"
        )
        if sem_resolucao:
            print(f"\n⚠️  {sum(sem_resolucao.values())} endereços ficam sem UF "
                  f"({len(sem_resolucao)} municípios distintos). Top 15:")
            for cidade, qtd in sem_resolucao.most_common(15):
                print(f"      {qtd:5d}  {cidade}")

        caminho = gravar_relatorio(mudancas, carimbo)
        print(f"\n📄 relatório: {caminho}")

        if args.apply:
            aplicar(conn, mudancas)
            print(f"✅ aplicado: {len(mudancas)} endereços atualizados.")
        else:
            print("ℹ️  dry-run: nada foi gravado (use --apply).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
