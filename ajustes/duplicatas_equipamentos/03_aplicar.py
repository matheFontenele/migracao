#!/usr/bin/env python3
"""Fase 3 — aplica o mapa canonico (dry-run por default) e desfaz lotes.

Uso:
    python ajustes/duplicatas_equipamentos/03_aplicar.py            # dry-run
    python ajustes/duplicatas_equipamentos/03_aplicar.py --apply
    python ajustes/duplicatas_equipamentos/03_aplicar.py --apply --somente-grupos 1,2
    python ajustes/duplicatas_equipamentos/03_aplicar.py --rollback out/run_X/auditoria.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    from . import aplicador, comum
except ImportError:  # execucao direta do script
    import aplicador
    import comum


def _lista_grupos(valor: str) -> list[int]:
    ids = []
    for parte in valor.replace(";", ",").split(","):
        parte = parte.strip()
        if parte:
            ids.append(int(parte))
    return ids


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fase 3: aplica o mapa canonico (ou reverte um lote).")
    parser.add_argument(
        "--mapa", default=str(comum.MAPA_PADRAO),
        help="mapa aprovado (default: mapa_canonico.csv).")
    parser.add_argument(
        "--rollback", default="",
        help="auditoria.csv de um lote a reverter (exige --apply).")
    parser.add_argument(
        "--apply", action="store_true",
        help="escreve no banco; sem esta flag tudo e dry-run.")
    parser.add_argument("--batch-id", default="", help="id do lote.")
    parser.add_argument(
        "--somente-grupos", "--somente-ids", dest="grupos", default="",
        help="lista de grupo_id separada por virgula (lote de teste).")
    parser.add_argument(
        "--limite", type=int, default=0, help="maximo de grupos do lote.")
    parser.add_argument(
        "--saida", default=str(comum.OUT), help="pasta de saida (default: out/).")
    args = parser.parse_args(argv)

    from config.database import obter_engines

    novo, _legado = obter_engines()
    dry_run = not args.apply

    if args.rollback:
        stats = aplicador.reverter(
            novo, Path(args.rollback), dry_run=dry_run)
        print(f"Rollback: {stats['revertidos']}/{stats['lidos']} revertidos"
              f"{' (dry-run)' if dry_run else ''}.")
        if stats["pulados"]:
            print(f"Pulados: {len(stats['pulados'])}")
        return 0

    stats = aplicador.aplicar(
        novo,
        mapa_csv=Path(args.mapa),
        batch_id=args.batch_id or None,
        somente_grupos=_lista_grupos(args.grupos) if args.grupos else None,
        limite=args.limite or None,
        dry_run=dry_run,
        pasta_saida=Path(args.saida),
    )
    print(f"\nLote {stats['batch_id']} {'(dry-run)' if dry_run else ''}: "
          f"{stats['grupos_selecionados']} grupos, {stats['descartados']} copias.")
    for status, total in sorted(stats["por_status"].items()):
        print(f"  {status}: {total}")
    print(f"Artefatos: {stats['pasta']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
