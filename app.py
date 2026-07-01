"""
API REST que serve os dados extraídos da agenda da Copa do Mundo 2026
da BBC Sport ( https://www.bbc.com/sport/football/world-cup/schedule ).

Recursos:
  * Cache em memória com TTL ( default 10 min ) para não martelar o site.
  * Respostas JSON padronizadas.

Execute:
    pip install -r requirements.txt
    python app.py
    # sirva em http://127.0.0.1:5000

Variáveis de ambiente:
    BBC_CACHE_TTL   segundos de cache (default 600)
    PORT            porta (default 5000)
"""
from __future__ import annotations

import os
import time
import threading
from flask import Flask, jsonify, request, Response

import scraper as scraper

app = Flask(__name__)

CACHE_TTL = int(os.environ.get("BBC_CACHE_TTL", "600"))
_cache_lock = threading.Lock()
_cache: dict[str, object] = {"data": None, "expires_at": 0.0, "site": "scheduled"}

def _get_data(force: bool = False) -> dict:
    """Retorna os dados parseados, usando cache TTL."""
    now = time.time()
    if (not force and _cache["data"] is not None
            and _cache["expires_at"] > now):
        return _cache["data"]  # type: ignore[return-value]
    with _cache_lock:
        if (not force and _cache["data"] is not None
                and _cache["expires_at"] > time.time()):
            return _cache["data"]  # type: ignore[return-value]
        html = scraper.fetch_html()
        _cache["data"] = scraper.parse_all(html)
        _cache["expires_at"] = time.time() + CACHE_TTL
        return _cache["data"]  # type: ignore[return-value]

def _find_group(data: dict, key: str):
    key = key.lower()
    for g in data["groups"]:
        if g["id"].lower() == key or g["name"].lower().replace("group ", "") == key:
            return g
    return None


# --------------------------------------------------------------------------- #
#  Endpoints
# --------------------------------------------------------------------------- #
@app.route("/")
def index() -> Response:
    return jsonify({
        "name": "BBC World Cup 2026 Schedule API",
        "source": scraper.SOURCE_URL,
        "endpoints": [
            "GET /api",
            "GET /api/groups",
            "GET /api/groups/<id>",
            "GET /api/third-place",
            "GET /api/knockout",
            "GET /api/knockout/<round>",
            "GET /api/matches",
            "GET /api/matches/<event_id>",
            "GET /api/search?team=<name>",
            "GET /health",
            "GET /refresh",
        ],
    })


@app.route("/api")
def full() -> Response:
    return jsonify(_get_data())


@app.route("/api/groups")
def groups() -> Response:
    data = _get_data()
    return jsonify({"groups": data["groups"]})


@app.route("/api/groups/<group_id>")
def one_group(group_id: str) -> Response:
    data = _get_data()
    g = _find_group(data, group_id)
    if g is None:
        return jsonify({"error": "Grupo não encontrado", "id": group_id}), 404
    return jsonify(g)


@app.route("/api/third-place")
def third_place() -> Response:
    data = _get_data()
    t = data.get("third_place_ranking")
    if t is None:
        return jsonify({"error": "Tabela de terceiros não disponível"}), 404
    return jsonify(t)


@app.route("/api/knockout")
def knockout() -> Response:
    data = _get_data()
    return jsonify({"rounds": data["knockout"]})


@app.route("/api/knockout/<round>")
def one_round(round: str) -> Response:
    data = _get_data()
    norm = round.lower().replace("-", " ").strip()
    aliases = {
        "last 32": "Last 32", "round of 32": "Last 32", "r32": "Last 32",
        "last 16": "Last 16", "round of 16": "Last 16", "r16": "Last 16",
        "quarter finals": "Quarter-finals", "quarterfinals": "Quarter-finals",
        "quarterfinal": "Quarter-finals", "qf": "Quarter-finals",
        "semi finals": "Semi-finals", "semifinals": "Semi-finals",
        "semifinal": "Semi-finals", "sf": "Semi-finals", "semis": "Semi-finals",
        "final": "Final", "third place": "Third Place", "3rd place": "Third Place",
    }
    canonical = aliases.get(norm, round)
    matches = data["knockout"].get(canonical)
    if matches is None:
        # tenta casar ignorando case
        for k in data["knockout"]:
            if k.lower() == canonical.lower():
                matches = data["knockout"][k]
                canonical = k
                break
    if matches is None:
        return jsonify({"error": "Rodada não encontrada",
                        "round": round,
                        "available": list(data["knockout"].keys())}), 404
    return jsonify({"round": canonical, "matches": matches})


@app.route("/api/matches")
def all_matches() -> Response:
    data = _get_data()
    return jsonify({"matches": data["matches"]})


@app.route("/api/matches/<event_id>")
def one_match(event_id: str) -> Response:
    data = _get_data()
    for m in data["matches"]:
        if m.get("event_id") == event_id:
            return jsonify(m)
    # alguns chamadores podem passar só a parte final do id
    for m in data["matches"]:
        if m.get("event_id") and m["event_id"].endswith(event_id):
            return jsonify(m)
    return jsonify({"error": "Partida não encontrada", "event_id": event_id}), 404


@app.route("/api/search")
def search() -> Response:
    team = request.args.get("team", "").strip().lower()
    if not team:
        return jsonify({"error": "Informe ?team=<nome>"}), 400
    data = _get_data()
    results = {"group_standings": [], "matches": []}

    for g in data["groups"]:
        for s in g["standings"]:
            if (team in s["team"].lower()
                    or (s.get("team_slug") and team in s["team_slug"].lower())):
                results["group_standings"].append({"group": g["id"], **s})

    if data.get("third_place_ranking"):
        for s in data["third_place_ranking"]["standings"]:
            if (team in s["team"].lower()
                    or (s.get("team_slug") and team in s["team_slug"].lower())):
                results["group_standings"].append({"group": "3rd", **s})

    for m in data["matches"]:
        if (team in m["home_team"].lower()
                or (m.get("home_slug") and team in m["home_slug"].lower())
                or team in m["away_team"].lower()
                or (m.get("away_slug") and team in m["away_slug"].lower())):
            results["matches"].append(m)

    return jsonify(results)


@app.route("/health")
def health() -> Response:
    return jsonify({
        "status": "ok",
        "cache_age_seconds": max(0, int(_cache["expires_at"] - time.time())),
        "cache_ttl": CACHE_TTL,
        "has_data": _cache["data"] is not None,
    })


@app.route("/refresh")
def refresh() -> Response:
    try:
        _get_data(force=True)
    except Exception as e:
        return jsonify({"error": "Falha ao atualizar", "detail": str(e)}), 500
    return jsonify({"refreshed": True, "cache_ttl": CACHE_TTL})


@app.errorhandler(Exception)
def handle_error(e):
    return jsonify({"error": "Erro interno", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)