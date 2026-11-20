"""CLI de importação das planilhas de contrato 2026 de Tauá (IP SERVIÇOS).

Por padrão roda em modo dry-run: lê as planilhas, valida totais, resolve os
clientes por alias e grava os relatórios em ``ajustes/``, sem tocar nos dados.
Use ``--apply`` para gravar (sempre em transação única).

Exemplos::

    .venv/bin/python -m cadastros.contratos.importar_planilhas_2026
    .venv/bin/python -m cadastros.contratos.importar_planilhas_2026 --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from config.database import obter_engines

from .planilhas_contrato_2026 import (
    carregar_planilhas,
    gerar_relatorio,
    importar_contratos,
    resolver_clientes,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Importa as planilhas de contrato 2026 de Tauá (dry-run por padrão).'
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Grava os contratos no banco. Sem esta opção apenas valida e relata.',
    )
    parser.add_argument(
        '--pasta',
        default=None,
        help='Pasta com as planilhas (padrão: docs/contratos).',
    )
    args = parser.parse_args(argv)

    contratos = (
        carregar_planilhas(args.pasta)
        if args.pasta
        else carregar_planilhas()
    )

    engine_new, _ = obter_engines()
    with engine_new.connect() as conn:
        resolver_clientes(conn, contratos)

    gerar_relatorio(contratos)

    if not args.apply:
        print('\n🔍 DRY-RUN: nenhuma alteração foi gravada no banco.')
        print('   Use --apply para executar a importação.')
        return 0

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with engine_new.begin() as conn:
        stats = importar_contratos(conn, contratos, now)

    # Relatório final com os ids efetivamente gravados.
    gerar_relatorio(contratos)
    print('\n✅ Importação concluída:')
    for chave in sorted(stats):
        print(f'   {chave}: {stats[chave]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
