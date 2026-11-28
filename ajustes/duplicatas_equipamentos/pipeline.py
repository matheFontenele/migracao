#!/usr/bin/env python3
"""Pipeline das Fases 0 e 1 — extracao + planejamento, nunca aplica.

Pensado para ser chamado pelo `main.py` (Fase 4) ou diretamente:

    from ajustes.duplicatas_equipamentos import pipeline
    pipeline.executar(eng_novo, eng_legado)
"""

from __future__ import annotations

import argparse

try:
    from . import comum, extrator, planejador
except ImportError:  # execucao direta do script
    import comum
    import extrator
    import planejador


def executar(eng_novo, eng_legado=None, argumentos_cli=None, log=print) -> dict:
    """Extrai os candidatos e gera o mapa. Nao escreve no banco."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--escopo", choices=[comum.ESCOPO_ORGAO, comum.ESCOPO_GLOBAL],
        default=comum.ESCOPO_ORGAO)
    parser.add_argument(
        "--regra", choices=[comum.REGRA_ATUAL, comum.REGRA_HISTORICO],
        default=comum.REGRA_ATUAL)
    parser.add_argument("--permitir-global", action="store_true")
    parser.add_argument("--aprovar-auto", action="store_true")
    parser.add_argument("--saida", default=str(comum.OUT))
    parser.add_argument("--mapa", default=str(comum.MAPA_PADRAO))
    args, _desconhecidos = parser.parse_known_args(argumentos_cli or [])

    from pathlib import Path

    pasta = Path(args.saida)
    extracao = extrator.extrair(eng_novo, escopo=args.escopo,
                                pasta_saida=pasta, log=log)
    planejamento = planejador.planejar(
        candidatos_csv=pasta / "candidatos.csv",
        mapa_saida=Path(args.mapa),
        escopo=args.escopo,
        regra=args.regra,
        permitir_global=args.permitir_global,
        aprovar_auto=args.aprovar_auto,
        engine=eng_novo,
        pasta_saida=pasta,
        log=log,
    )
    return {
        "extracao": extracao,
        "planejamento": planejamento,
        "mapa": args.mapa,
        "dry_run": True,
    }


def main(argv=None) -> int:
    from config.database import obter_engines

    novo, legado = obter_engines()
    stats = executar(novo, legado, argumentos_cli=argv)
    print(f"\nGrupos: {stats['planejamento']['grupos']} | "
          f"travados: {stats['planejamento']['grupos_travados']} | "
          f"descartaveis: {stats['planejamento']['grupos_descartaveis']}")
    print(f"Mapa: {stats['mapa']}")
    print(f"Hash: {stats['planejamento']['hash_mapa']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
