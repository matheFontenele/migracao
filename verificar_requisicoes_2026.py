#!/usr/bin/env python3
"""Confere a importação das requisições 2026 já gravada no banco.

Checa contagens, duplicidade de cabeçalho (mesma chave do agrupador), itens
órfãos, cabeçalhos sem item e o protocolo dos movimentos (`movements.number`).
Sai com código 1 se algo divergir.

O protocolo era a lacuna do fluxo: o INSERT das requisições grava `number`
NULL porque não passa pelo hook `MovementNumberSequence` do Laravel. Este
verificador falha se sobrar movimento sem número ou se a sequência
(`movement_number_sequences.last_number`) ficar atrás do MAX(number).

Uso:
    .venv/bin/python verificar_requisicoes_2026.py
    .venv/bin/python verificar_requisicoes_2026.py --sem-contagem-fixa
"""
from __future__ import annotations

import argparse

from sqlalchemy import create_engine, text

MARCADOR = "Migração REQUISIÇÃO 2026.csv"
# Contagens após a unificação das prefeituras (Tauá e Aquiraz com o cadastro
# raiz 903 anexado). O agrupamento usa `cliente_id`: como as secretarias de
# Aquiraz passam a ser endereços de uma raiz única, 16 grupos de cabeçalho se
# fundem (os mesmos 16 de nível 3/approved) e 5 deles deixam de gerar
# movimento. Itens não se alteram (1.074 / 1.271). A granularidade anterior
# (um customer por setor legado) é reproduzida linha a linha e dá 453/385.
#
# Em 2026-11-24 a planilha de entrada foi atualizada com lançamentos até
# 23/11 (antes ia até 12/11): +9 requisições, +18 itens, +17 movimentos e
# +108 itens de movimento. As contagens abaixo refletem essa carga.
#
# Ainda em 2026-11-24 passou a ser ignorado o cliente SECRETARIA DE SAUDE DE
# SÃO LUIS (255 linhas, todas de 23/11): -1 requisição, -7 itens, -2 movimentos
# e -82 itens de movimento. As demais unidades de São Luís continuam migrando.
ESPERADO = {"requisicoes": 445, "itens": 1085, "movimentos": 395, "itens_movimento": 1297}


def verificar(url: str, contagem_fixa: bool = True) -> int:
    eng = create_engine(url)
    with eng.connect() as c:
        def contar(sql: str, **extra) -> int:
            return int(c.execute(text(sql), {"m": f"%{MARCADOR}%", **extra}).scalar())

        total = {
            "requisicoes": contar(
                "SELECT COUNT(*) FROM service_orders WHERE details LIKE :m"),
            "itens": contar(
                "SELECT COUNT(*) FROM service_order_items WHERE service_order_id IN "
                "(SELECT id FROM service_orders WHERE details LIKE :m)"),
            "movimentos": contar(
                "SELECT COUNT(*) FROM movements WHERE details LIKE :m"),
            "itens_movimento": contar(
                "SELECT COUNT(*) FROM movement_items WHERE movement_id IN "
                "(SELECT id FROM movements WHERE details LIKE :m)"),
        }
        # Chave do agrupador: cliente + usuário + organização + envio + data + previsão.
        duplicados = c.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT recipient_customer_id, user_id, organization_id,
                       mode_transport_id, DATE(created_at) dia, DATE(deadline) prazo,
                       COUNT(*) q
                FROM service_orders WHERE details LIKE :m
                GROUP BY recipient_customer_id, user_id, organization_id,
                         mode_transport_id, DATE(created_at), DATE(deadline)
                HAVING q > 1
            ) t
        """), {"m": f"%{MARCADOR}%"}).scalar()
        orfaos = c.execute(text("""
            SELECT COUNT(*) FROM service_order_items soi
            LEFT JOIN service_orders so ON so.id = soi.service_order_id
            WHERE soi.service_order_id IN (
                SELECT id FROM service_orders WHERE details LIKE :m)
              AND so.id IS NULL
        """), {"m": f"%{MARCADOR}%"}).scalar()
        sem_item = c.execute(text("""
            SELECT COUNT(*) FROM service_orders so
            WHERE so.details LIKE :m
              AND NOT EXISTS (SELECT 1 FROM service_order_items soi
                              WHERE soi.service_order_id = so.id)
        """), {"m": f"%{MARCADOR}%"}).scalar()

        sem_protocolo = c.execute(text(
            "SELECT COUNT(*) FROM movements WHERE details LIKE :m "
            "AND (`number` IS NULL OR `number` = '')"),
            {"m": f"%{MARCADOR}%"}).scalar_one()
        protocolos_duplicados = c.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT `number` n, COUNT(*) q FROM movements
                WHERE details LIKE :m AND `number` IS NOT NULL AND `number` <> ''
                GROUP BY `number` HAVING q > 1
            ) t
        """), {"m": f"%{MARCADOR}%"}).scalar_one()
        maximo = int(c.execute(text(
            "SELECT COALESCE(MAX(CAST(`number` AS UNSIGNED)), 0) FROM `movements` "
            "WHERE `number` IS NOT NULL AND `number` <> ''")).scalar() or 0)
        sequencia = c.execute(text(
            "SELECT `last_number` FROM `movement_number_sequences` WHERE `id` = 1"
        )).scalar()
        itens_sem_departamento = c.execute(text("""
            SELECT COUNT(*) FROM service_order_items soi
            WHERE soi.service_order_id IN (
                SELECT id FROM service_orders WHERE details LIKE :m)
              AND soi.department_id IS NULL
        """), {"m": f"%{MARCADOR}%"}).scalar_one()
        por_departamento = c.execute(text("""
            SELECT d.`name`, COUNT(*) FROM service_order_items soi
            JOIN service_order_departments d ON d.`id` = soi.`department_id`
            WHERE soi.service_order_id IN (
                SELECT id FROM service_orders WHERE details LIKE :m)
            GROUP BY d.`name` ORDER BY d.`name`
        """), {"m": f"%{MARCADOR}%"}).fetchall()

    print("requisicoes:", {k: f"{v:,}" for k, v in total.items()})
    print("cabecalhos duplicados:", duplicados, "| itens orfaos:", orfaos,
          "| cabecalhos sem item:", sem_item)
    print("movimentos sem protocolo:", sem_protocolo,
          "| protocolos duplicados:", protocolos_duplicados)
    print("MAX(number):", f"{maximo:,}", "| sequence last_number:",
          f"{int(sequencia):,}" if sequencia is not None else "ausente")
    print("itens sem departamento:", itens_sem_departamento,
          "| por departamento:",
          {nome: f"{qtd:,}" for nome, qtd in por_departamento})

    divergentes = []
    if contagem_fixa:
        divergentes = [k for k, v in total.items() if v != ESPERADO[k]]
    problemas = []
    if divergentes:
        problemas.append("contagens")
    if duplicados or orfaos or sem_item:
        problemas.append("integridade")
    if sem_protocolo:
        problemas.append("movimentos sem protocolo")
    if protocolos_duplicados:
        problemas.append("protocolos duplicados")
    if sequencia is None or int(sequencia) < maximo:
        problemas.append("sequencia atrasada")
    if itens_sem_departamento:
        problemas.append("itens sem departamento")

    if problemas:
        print("DIVERGENCIA em:", ", ".join(problemas))
        return 1
    print("REQUISICOES OK (contagens, integridade, protocolos e sequencia conferem)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="mysql+pymysql://root:root@127.0.0.1:3307/"
                                   "controle-interno?charset=utf8mb4")
    p.add_argument("--sem-contagem-fixa", action="store_true",
                   help="Não compara com as contagens esperadas da planilha.")
    args = p.parse_args()
    return verificar(args.url, contagem_fixa=not args.sem_contagem_fixa)


if __name__ == "__main__":
    raise SystemExit(main())
