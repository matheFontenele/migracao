"""Front somente de consulta ao banco legado.

Não existe revisão, aprovação, dry-run, aplicação nem rollback aqui: todas as
rotas apenas leem o legado. O pipeline de ajuste continua disponível como CLI
em `ajustes/duplicatas_equipamentos`.
"""
import os

from flask import Flask, jsonify, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from config.database import obter_engines

from . import servico_consulta

_engines = None


def _engines_atuais():
    global _engines
    if _engines is None:
        _engines = obter_engines()
    return _engines


def _legado():
    return _engines_atuais()[1]


def _erro(codigo, mensagem, status, **extra):
    return jsonify({"erro": codigo, "mensagem": mensagem, **extra}), status


def _int(value):
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _args_consulta():
    """Lê e valida os filtros da consulta; devolve (valores, erro)."""
    args = request.args
    valores = {}
    for chave in ("numero", "orgao_id", "tipo_id", "situacao_id"):
        bruto = str(args.get(chave, "")).strip()
        if not bruto:
            valores[chave] = None
            continue
        valor = _int(bruto)
        if valor is None:
            return None, _erro("filtro_invalido", f"{chave} inválido", 422)
        valores[chave] = valor
    valores["descricao"] = str(args.get("descricao", "")).strip() or None
    valores["somente_duplicados"] = str(
        args.get("somente_duplicados", "")
    ).lower() in ("1", "true", "sim", "s")
    valores["page"] = _int(args.get("page")) or 1
    valores["per_page"] = _int(args.get("per_page")) or 24
    return valores, None


def criar_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.json.ensure_ascii = False

    @app.errorhandler(SQLAlchemyError)
    def db_error(exc):
        return _erro("banco_indisponivel", str(exc), 503)

    # --- páginas (a navegação é feita no cliente) ---
    @app.get("/")
    def raiz():
        return redirect(url_for("consulta_page"))

    @app.get("/consulta")
    def consulta_page():
        return render_template("consulta.html", numero=None, legado_id=None)

    @app.get("/consulta/tombo/<int:numero>")
    def consulta_tombo_page(numero):
        return render_template("consulta.html", numero=numero, legado_id=None)

    @app.get("/consulta/equipamento/<int:legado_id>")
    def consulta_equipamento_page(legado_id):
        return render_template("consulta.html", numero=None, legado_id=legado_id)

    @app.get("/consulta/<int:legado_id>")
    def consulta_antiga(legado_id):
        destino = url_for("consulta_equipamento_page", legado_id=legado_id)
        if request.query_string:
            destino += "?" + request.query_string.decode()
        return redirect(destino)

    # --- APIs de consulta ---
    @app.get("/api/saude")
    def saude():
        legado = "ok"
        try:
            _legado().connect().close()
        except Exception as exc:  # pragma: no cover - depende do ambiente
            legado = "erro: " + str(exc)[:160]
        return jsonify(ok=legado == "ok", legado=legado, modo="consulta")

    @app.get("/api/consulta/filtros")
    def consulta_filtros():
        return jsonify(servico_consulta.filtros(_legado()))

    @app.get("/api/consulta/tombos")
    def consulta_tombos():
        valores, erro = _args_consulta()
        if erro:
            return erro
        try:
            return jsonify(servico_consulta.tombos(_legado(), **valores))
        except SQLAlchemyError:
            raise
        except Exception as exc:
            return _erro("consulta_falhou", str(exc)[:200], 500)

    @app.get("/api/consulta/tombos/<int:numero>")
    def consulta_tombo(numero):
        dado = servico_consulta.tombo(_legado(), numero)
        if not dado:
            return _erro("tombo_inexistente", "Tombo não encontrado no legado", 404)
        return jsonify(dado)

    @app.get("/api/consulta/equipamentos/<int:legado_id>")
    def consulta_equipamento(legado_id):
        dado = servico_consulta.detalhe(_legado(), legado_id)
        if not dado:
            return _erro("equipamento_inexistente",
                         "Equipamento não encontrado no legado", 404)
        return jsonify(dado)

    @app.get("/api/consulta/movimentos/<int:movimento_id>")
    def consulta_movimento(movimento_id):
        dado = servico_consulta.movimento(_legado(), movimento_id)
        if not dado:
            return _erro("movimento_inexistente",
                         "Movimento não encontrado no legado", 404)
        return jsonify(dado)

    return app


app = criar_app()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("FLASK_PORT", "5000")), debug=False)
