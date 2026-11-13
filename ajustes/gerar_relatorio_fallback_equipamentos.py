#!/usr/bin/env python3
"""Gera planilha para revisão manual dos fallbacks de equipamentos."""

import argparse
from pathlib import Path

import pandas as pd


COLUNAS_OBRIGATORIAS = {
    "id_legado", "tombo", "nome", "tipo", "org_destino", "grupo", "marca",
    "grupo_fallback", "marca_fallback", "situacao_id_legado",
}


def gerar_relatorio(
    arquivo_csv: Path,
    arquivo_xlsx: Path,
    arquivo_fornecedores_csv: Path | None = None,
) -> dict[str, int]:
    df = pd.read_csv(arquivo_csv, encoding="utf-8-sig", dtype={"tombo": "string"})
    ausentes = COLUNAS_OBRIGATORIAS - set(df.columns)
    if ausentes:
        raise ValueError(f"Colunas ausentes no CSV: {', '.join(sorted(ausentes))}")

    # Os relatórios anteriores não registravam explicitamente o valor usado
    # como fornecedor fallback nem o tipo original do legado.
    if "fornecedor_fallback" not in df.columns:
        df["fornecedor_fallback"] = df["marca"].where(df["marca_fallback"].eq(1), "")
    if "tipo_legado" not in df.columns:
        df["tipo_legado"] = df["tipo"]

    if arquivo_fornecedores_csv is None:
        arquivo_fornecedores_csv = arquivo_csv.with_name("fallback_fornecedor_alcom.csv")
    if arquivo_fornecedores_csv.exists():
        fornecedores_alcom = pd.read_csv(arquivo_fornecedores_csv, encoding="utf-8-sig", dtype={"tombo": "string"})
        aliases = {"tombo_legado": "tombo", "nome_legado": "nome", "tipo_legado": "tipo", "fornecedor": "marca"}
        fornecedores_alcom.rename(columns={k: v for k, v in aliases.items() if k in fornecedores_alcom.columns and v not in fornecedores_alcom.columns}, inplace=True)
        colunas_fornecedor = ["id_legado", "tombo", "nome", "tipo", "org_destino", "grupo", "marca"]
        for coluna in colunas_fornecedor:
            if coluna not in fornecedores_alcom.columns:
                fornecedores_alcom[coluna] = ""
        fornecedores_alcom = fornecedores_alcom[colunas_fornecedor].copy()
        fornecedores_alcom.insert(0, "motivo_fallback", "Fornecedor sem correspondência; associado ao fornecedor ALUCOM")
    else:
        fornecedores_alcom = pd.DataFrame(columns=["motivo_fallback", "id_legado", "tombo", "nome", "tipo", "org_destino", "grupo", "marca"])

    grupo_fallback = pd.to_numeric(df["grupo_fallback"], errors="coerce").eq(1)
    fornecedor_fallback = pd.to_numeric(df["marca_fallback"], errors="coerce").eq(1)
    campos = ["id_legado", "tombo", "nome", "tipo", "tipo_legado", "org_destino", "grupo", "marca", "fornecedor_fallback", "situacao_id_legado"]

    grupos = df.loc[grupo_fallback, campos].copy()
    fornecedores = df.loc[fornecedor_fallback, campos].copy()
    grupos.insert(0, "motivo_fallback", "Grupo ausente no mapeamento; aplicado GERAL")
    fornecedor_aplicado = str(fornecedores["fornecedor_fallback"].dropna().iloc[0]) if not fornecedores["fornecedor_fallback"].dropna().empty else "ALUCOM"
    fornecedores.insert(0, "motivo_fallback", f"Fornecedor ausente na planilha auxiliar; aplicado {fornecedor_aplicado}")
    grupos.rename(columns={"grupo": "grupo_aplicado"}, inplace=True)
    fornecedores.rename(columns={"marca": "marca_planilha", "fornecedor_fallback": "fornecedor_aplicado"}, inplace=True)

    # Aba principal: uma linha por registro legado afetado, para que a planilha
    # abra diretamente na lista revisável e não apenas no resumo de contagens.
    produtos_legado = df.loc[grupo_fallback | fornecedor_fallback, [
        "id_legado", "tombo", "nome", "tipo", "tipo_legado", "org_destino", "grupo", "marca",
        "situacao_id_legado", "grupo_fallback", "marca_fallback", "fornecedor_fallback",
    ]].copy()
    produtos_legado.rename(columns={
        "grupo": "grupo_aplicado",
        "marca": "fornecedor_aplicado",
        "fornecedor_fallback": "fornecedor_fallback_aplicado",
        "grupo_fallback": "fallback_grupo",
        "marca_fallback": "fallback_fornecedor",
    }, inplace=True)
    produtos_legado.insert(0, "motivo_fallback", produtos_legado.apply(
        lambda r: "; ".join(
            motivo for ativo, motivo in [
                (r["fallback_grupo"] == 1, "Grupo ausente; aplicado GERAL"),
                (r["fallback_fornecedor"] == 1, f"Fornecedor ausente; aplicado {r['fornecedor_fallback_aplicado']}"),
            ] if ativo
        ), axis=1
    ))

    resumo = pd.DataFrame([
        {"tipo_fallback": "Grupo", "valor_aplicado": "GERAL", "quantidade": len(grupos)},
        {"tipo_fallback": "Fornecedor", "valor_aplicado": fornecedor_aplicado, "quantidade": len(fornecedores)},
        {"tipo_fallback": "Fornecedor sem correspondência", "valor_aplicado": "ALUCOM", "quantidade": len(fornecedores_alcom)},
    ])
    arquivo_xlsx.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(arquivo_xlsx, engine="openpyxl") as writer:
        produtos_legado.to_excel(writer, sheet_name="Produtos legados", index=False)
        resumo.to_excel(writer, sheet_name="Resumo", index=False)
        grupos.to_excel(writer, sheet_name="Fallback grupo", index=False)
        fornecedores.to_excel(writer, sheet_name="Fallback fornecedor", index=False)
        fornecedores_alcom.to_excel(writer, sheet_name="Fallback ALUCOM", index=False)
        for sheet in writer.book.worksheets:
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for column in sheet.columns:
                width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 55)
                sheet.column_dimensions[column[0].column_letter].width = width
    return {"grupo": len(grupos), "fornecedor": len(fornecedores), "fornecedor_alcom": len(fornecedores_alcom), "produtos_legado": len(produtos_legado)}


def main() -> None:
    raiz = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=raiz / "ajustes/fallback_equipamentos.csv")
    parser.add_argument("--xlsx", type=Path, default=raiz / "ajustes/relatorio_fallback_equipamentos.xlsx")
    args = parser.parse_args()
    contagens = gerar_relatorio(args.csv, args.xlsx)
    print(
        f"Relatório salvo em {args.xlsx}: produtos legados={contagens['produtos_legado']}, "
        f"grupo={contagens['grupo']}, fornecedor={contagens['fornecedor']}, "
        f"fornecedor ALUCOM={contagens['fornecedor_alcom']}"
    )


if __name__ == "__main__":
    main()
