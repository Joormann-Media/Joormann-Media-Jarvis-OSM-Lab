import os
import subprocess
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"

for env_file in (CONFIG_DIR / "ports.env", CONFIG_DIR / "osm.env", BASE_DIR / ".env"):
    if env_file.exists():
        load_dotenv(env_file)

FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5079"))
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "http://localhost:7071").rstrip("/")
ORS_URL = os.environ.get("ORS_URL", "http://localhost:8082").rstrip("/")
GRAPHHOPPER_URL = os.environ.get("GRAPHHOPPER_URL", "http://localhost:8989").rstrip("/")
TILESERVER_URL = os.environ.get("TILESERVER_URL", "http://localhost:8083").rstrip("/")
VROOM_URL = os.environ.get("VROOM_URL", "http://localhost:8084").rstrip("/")
OSM_COMPOSE_DIR = os.environ.get("OSM_COMPOSE_DIR", "/home/djanebmb/osm-neu")

TIMEOUT = 3

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SERVICES = [
    {"key": "nominatim", "name": "Nominatim (Geocoding)", "url": NOMINATIM_URL + "/status", "container": "nominatim-nrw"},
    {"key": "ors",       "name": "OpenRouteService",       "url": ORS_URL + "/ors/v2/health",  "container": "ors-app"},
    {"key": "graphhopper","name": "GraphHopper",            "url": GRAPHHOPPER_URL + "/health",  "container": "graphhopper"},
    {"key": "tileserver","name": "TileServer GL",           "url": TILESERVER_URL + "/health",   "container": "tileserver"},
    {"key": "vroom",     "name": "VROOM",                   "url": VROOM_URL + "/",              "container": "vroom"},
]


def _http_ok(url: str) -> tuple[bool, int]:
    try:
        r = requests.get(url, timeout=TIMEOUT)
        return r.status_code < 500, r.status_code
    except Exception:
        return False, 0


def _docker_state(container: str) -> str:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "not found"
    except Exception:
        return "error"


def _nominatim_progress() -> dict:
    """
    Prüft ob Nominatim bereits Anfragen beantwortet.
    Während des Imports antwortet /status mit HTTP 503 und einer Statusmeldung.
    """
    try:
        r = requests.get(NOMINATIM_URL + "/status.php", timeout=TIMEOUT)
        if r.status_code == 200:
            return {"phase": "ready", "detail": "Import abgeschlossen – bereit"}
        if r.status_code == 503:
            return {"phase": "importing", "detail": r.text.strip()[:200] or "Import läuft…"}
        return {"phase": "unknown", "detail": f"HTTP {r.status_code}"}
    except requests.exceptions.ConnectionError:
        state = _docker_state("nominatim-nrw")
        if state == "running":
            return {"phase": "starting", "detail": "Container läuft, HTTP noch nicht erreichbar"}
        return {"phase": state, "detail": f"Container: {state}"}
    except Exception as e:
        return {"phase": "error", "detail": str(e)}


def _build_status() -> dict:
    services = []
    for svc in SERVICES:
        ok, code = _http_ok(svc["url"])
        docker = _docker_state(svc["container"])
        entry = {
            "key": svc["key"],
            "name": svc["name"],
            "http_ok": ok,
            "http_code": code,
            "docker": docker,
        }
        if svc["key"] == "nominatim":
            entry["nominatim"] = _nominatim_progress()
        services.append(entry)
    return {"services": services}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html",
                           tileserver_url=TILESERVER_URL,
                           nominatim_url=NOMINATIM_URL)


@app.route("/status")
def status_page():
    return render_template("status.html")


@app.route("/api/status")
def api_status():
    return jsonify(_build_status())


@app.route("/api/geocode", methods=["POST"])
def api_geocode():
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Kein Suchbegriff angegeben"}), 400

    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": int(data.get("limit", 10)),
        "accept-language": "de",
    }
    try:
        r = requests.get(NOMINATIM_URL + "/search", params=params, timeout=TIMEOUT,
                         headers={"User-Agent": "Joormann-OSM-Lab/1.0"})
        r.raise_for_status()
        return jsonify({"results": r.json()})
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Nominatim nicht erreichbar"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reverse", methods=["POST"])
def api_reverse():
    data = request.get_json(force=True, silent=True) or {}
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat und lon als Dezimalzahl erforderlich"}), 400

    params = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "addressdetails": 1,
        "zoom": int(data.get("zoom", 18)),
        "accept-language": "de",
    }
    try:
        r = requests.get(NOMINATIM_URL + "/reverse", params=params, timeout=TIMEOUT,
                         headers={"User-Agent": "Joormann-OSM-Lab/1.0"})
        r.raise_for_status()
        return jsonify(r.json())
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Nominatim nicht erreichbar"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)
