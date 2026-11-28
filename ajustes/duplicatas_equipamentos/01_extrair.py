#!/usr/bin/env python3
"""Fase 0 — extracao somente leitura dos candidatos a duplicata.

Uso:
    python ajustes/duplicatas_equipamentos/01_extrair.py --escopo orgao
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from . import comum, extrator
except ImportError:  # execucao direta do script
    import comum
    import extrator


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fase 0: le o banco novo e grava out/candidatos.csv.")
    parser.add_argument(
        "--escopo", choices=[comum.ESCOPO_ORGAO, comum.ESCOPO_GLOBAL],
        default=comum.ESCOPO_ORGAO,
        help="orgao (default, por number+organizacao) ou global (por number).")
    parser.add_argument(
        "--saida", default=str(comum.OUT), help="pasta de saida (default: out/).")
    args = parser.parse_args(argv)

    from config.database import obter_engines

    novo, _legado = obter_engines()
    stats = extrator.extrair(novo, escopo=args.escopo,
                             pasta_saida=Path(args.saida))
    print(f"\nCandidatos: {stats['linhas']} linhas em {stats['grupos']} grupos "
          f"({stats['excedentes']} excedentes).")
    print(f"Saida: {Path(args.saida) / 'candidatos.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
