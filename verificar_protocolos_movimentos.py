"""Conferência de protocolos duplicados em `movements.number`.

O `number` de um movimento migrado é o id do movimento no legado. Dois módulos
da esteira não devem gerar duas capas para o mesmo número — quando isso
acontece, o protocolo aparece duplicado na tela de movimentos.

Uso:
    .venv/bin/python verificar_protocolos_movimentos.py
    .venv/bin/python verificar_protocolos_movimentos.py --limite 0   # falha se houver duplicata

Saída: total de movimentos, números duplicados e o cruzamento dos prefixos de
`details` que dividem o mesmo número (para saber de qual etapa vem a colisão).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

from sqlalchemy import text

from config.database import obter_engines

PREFIXO = (
    "Gerado por migração",
    "Aluguel Passado",
    "Migração (Reconstrução)",
    "Migração: Devolução",
    "Devolução da Substituição",
    "Envio da Substituição",
    "Ajuste de migração individual",
    "Migração - Reserva",
    "Migração requisição",
    "Migração REQUISIÇÃO",
)


def rotulo(details: str | None) -> str:
    texto = (details or "").strip()
    if not texto:
        return "(sem detalhe)"
    for prefixo in PREFIXO:
        if texto.startswith(prefixo):
            return prefixo
    return texto[:32]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limite", type=int, default=None,
                        help="falha (exit 1) se houver mais de N números duplicados")
    args = parser.parse_args()

    engines = obter_engines()
    engine = engines["novo"] if isinstance(engines, dict) else engines[0]

    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM movements")).scalar_one()
        duplicados = conn.execute(text("""
            SELECT number FROM movements
            WHERE number IS NOT NULL AND number <> ''
            GROUP BY number HAVING COUNT(*) > 1
        """)).scalars().all()
        linhas = conn.execute(text("""
            SELECT m.number, m.details
            FROM movements m
            WHERE m.number IN (
                SELECT number FROM movements
                WHERE number IS NOT NULL AND number <> ''
                GROUP BY number HAVING COUNT(*) > 1
            )
        """)).all()

    por_numero: dict[str, Counter] = defaultdict(Counter)
    for numero, details in linhas:
        por_numero[str(numero)][rotulo(details)] += 1

    combinacoes: Counter = Counter(
        tuple(sorted(contagem)) for contagem in por_numero.values())

    print(f"Movimentos:            {total:,}")
    print(f"Números duplicados:    {len(duplicados):,}")
    if combinacoes:
        print("\nColisões por origem (details):")
        for combinacao, quantidade in combinacoes.most_common(15):
            print(f"  {quantidade:>6,} nº  " + "  ×  ".join(combinacao))

    if args.limite is not None and len(duplicados) > args.limite:
        print(f"\n❌ Acima do limite de {args.limite} número(s) duplicado(s).")
        return 1
    print("\n✅ Conferência concluída.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
