"""Runner interativo do aluguel avulso CO Brasil no banco refatorado."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.database import (  # noqa: E402
    obter_engine_local_refatorado,
    obter_engine_producao,
)
from movimentos.migracao_aluguel_co_brasil import (  # noqa: E402
    apply_groups,
    load_lookups,
    load_source,
    print_preview,
    resolve_groups,
    write_preview_csv,
)

SOURCE = ROOT / "docs" / "co_brasil.csv"
OUTPUT_DIR = Path("/tmp/opencode/aluguel-co-brasil")
PRODUCTION_DATABASE = "controle-interno"


def select_target():
    print("Destino do aluguel avulso CO Brasil:")
    print("1) Refatorado local")
    print("2) Refatorado produção")
    choice = input("Escolha [1/2]: ").strip()
    if choice == "1":
        engine = obter_engine_local_refatorado()
        host = os.environ["LOCAL_DB_HOST"]
        port = os.environ["LOCAL_DB_PORT"]
        return f"Refatorado local (controle-interno em {host}:{port})", engine
    if choice != "2":
        raise ValueError("Escolha inválida; informe 1 ou 2.")

    engine = obter_engine_producao()
    host = os.environ["PROD_DB_HOST"]
    port = os.environ["PROD_DB_PORT"]
    return f"Refatorado produção ({PRODUCTION_DATABASE} em {host}:{port})", engine


def main() -> None:
    engine = None
    try:
        target, engine = select_target()
        print(f"\nDestino selecionado: {target}")
        with engine.connect() as conn:
            identity = conn.execute(
                text("SELECT DATABASE(), @@hostname, @@port")
            ).one()
            database, server_host, server_port = identity
            print(
                f"Identidade consultada: DATABASE()={database!r}, "
                f"@@hostname={server_host!r}, @@port={server_port}"
            )
            if database != PRODUCTION_DATABASE:
                raise ValueError(f"Conexão selecionada aponta para {database!r}, esperado {PRODUCTION_DATABASE!r}.")
            lookups = load_lookups(conn)
        groups, skipped, errors = resolve_groups(load_source(SOURCE), lookups)
        print_preview(groups, skipped, errors, apply_hint=False)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        preview = OUTPUT_DIR / "preview_aluguel_co_brasil.csv"
        write_preview_csv(preview, groups, skipped)
        print(f"Prévia: {preview}")
        if errors or skipped or not groups:
            print("Execução bloqueada: corrija os erros/ignorados ou não há itens válidos.")
            return
        confirmation = "CONFIRMO PRODUCAO" if target.startswith("Refatorado produção") else "CONFIRMO LOCAL-REFATORADO"
        print(f"Destino: {target}\nIdentidade: DATABASE()={database!r}, @@hostname={server_host!r}, @@port={server_port}")
        print(f"Para efetivar as alterações, digite exatamente: {confirmation}")
        if input("> ").strip() != confirmation:
            print("Cancelado; nenhuma gravação realizada.")
            return
        totals = apply_groups(engine, groups, lookups)
        print("APLICADO: " + "; ".join(f"{key}={value}" for key, value in totals.items()))
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, EOFError) as error:
        print(f"ERRO: {error}", file=sys.stderr)
        raise SystemExit(1)
