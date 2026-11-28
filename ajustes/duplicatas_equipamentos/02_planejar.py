#!/usr/bin/env python3
"""Fase 1 — gera o mapa canonico, o impacto e os conflitos (somente leitura).

Uso:
    python ajustes/duplicatas_equipamentos/02_planejar.py --regra atual
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from . import comum, planejador
except ImportError:  # execucao direta do script
    import comum
    import planejador


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fase 1: aplica a regra do canonico e grava o mapa.")
    parser.add_argument(
        "--escopo", choices=[comum.ESCOPO_ORGAO, comum.ESCOPO_GLOBAL],
        default=comum.ESCOPO_ORGAO)
    parser.add_argument(
        "--regra", choices=[comum.REGRA_ATUAL, comum.REGRA_HISTORICO],
        default=comum.REGRA_ATUAL)
    parser.add_argument(
        "--candidatos", default=str(comum.OUT / "candidatos.csv"),
        help="CSV da Fase 0.")
    parser.add_argument(
        "--mapa", default=str(comum.MAPA_PADRAO),
        help="mapa de saida (default: mapa_canonico.csv na pasta do modulo).")
    parser.add_argument(
        "--permitir-global", action="store_true",
        help="obrigatorio no escopo global; o global nunca e aplicado.")
    parser.add_argument(
        "--aprovar-auto", action="store_true",
        help="marca aprovado=S/revisado_por=auto nos grupos sem revisao "
             "obrigatoria (use apenas apos conferir o revisao.md).")
    parser.add_argument(
        "--saida", default=str(comum.OUT), help="pasta de saida (default: out/).")
    args = parser.parse_args(argv)

    from config.database import obter_engines

    novo, _legado = obter_engines()
    stats = planejador.planejar(
        candidatos_csv=Path(args.candidatos),
        mapa_saida=Path(args.mapa),
        escopo=args.escopo,
        regra=args.regra,
        permitir_global=args.permitir_global,
        aprovar_auto=args.aprovar_auto,
        engine=novo,
        pasta_saida=Path(args.saida),
    )

    print(f"\nGrupos: {stats['grupos']} | travados: {stats['grupos_travados']} "
          f"| descartaveis: {stats['grupos_descartaveis']}")
    print("Acoes: " + ", ".join(f"{k}={v}" for k, v in stats["por_acao"].items()))
    print(f"Mapa: {args.mapa}")
    print(f"Hash: {stats['hash_mapa']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
