import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

BASE_DIR   = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
DOCKER_DIR = BASE_DIR / "docker"


def _load_env():
    for env_file in (CONFIG_DIR / "ports.env", CONFIG_DIR / "osm.env", BASE_DIR / ".env"):
        if env_file.exists():
            load_dotenv(env_file, override=True)


_load_env()

FLASK_HOST  = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT  = int(os.environ.get("FLASK_PORT", "5079"))
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

NOMINATIM_URL     = os.environ.get("NOMINATIM_URL",     "http://localhost:7071").rstrip("/")
ORS_URL           = os.environ.get("ORS_URL",           "http://localhost:8082").rstrip("/")
GRAPHHOPPER_URL   = os.environ.get("GRAPHHOPPER_URL",   "http://localhost:8989").rstrip("/")
TILESERVER_URL    = os.environ.get("TILESERVER_URL",    "http://localhost:8083").rstrip("/")
VROOM_URL         = os.environ.get("VROOM_URL",         "http://localhost:8084").rstrip("/")
OVERPASS_URL      = os.environ.get("OVERPASS_URL",      "http://localhost:7072").rstrip("/")
OSM_DATA_ROOT     = os.environ.get("OSM_DATA_ROOT",     "/mnt/data_toshiba")
GEOFABRIK_PBF_URL = os.environ.get("GEOFABRIK_PBF_URL",
    "https://download.geofabrik.de/europe/germany/nordrhein-westfalen-latest.osm.pbf")
MBTILES_URL       = os.environ.get("MBTILES_URL", "")

TIMEOUT = 3

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

SERVICES = [
    {"key": "nominatim",   "name": "Nominatim (Geocoding)", "url": lambda: NOMINATIM_URL   + "/status.php",        "container": "nominatim-nrw"},
    {"key": "overpass",    "name": "Overpass API (POI)",    "url": lambda: OVERPASS_URL    + "/api/interpreter",   "container": "overpass-nrw"},
    {"key": "ors",         "name": "OpenRouteService",       "url": lambda: ORS_URL         + "/ors/v2/health",     "container": "ors-app"},
    {"key": "graphhopper", "name": "GraphHopper",            "url": lambda: GRAPHHOPPER_URL + "/health",            "container": "graphhopper"},
    {"key": "tileserver",  "name": "TileServer GL",          "url": lambda: TILESERVER_URL  + "/health",            "container": "tileserver"},
    {"key": "vroom",       "name": "VROOM",                  "url": lambda: VROOM_URL       + "/health",            "container": "vroom"},
]


def _http_ok(url: str) -> tuple[bool, int]:
    try:
        r = requests.get(url, timeout=TIMEOUT)
        return r.status_code < 500, r.status_code
    except Exception:
        return False, 0


def _docker_state(container: str) -> str:
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else "not found"
    except Exception:
        return "error"


def _nominatim_progress() -> dict:
    try:
        r = requests.get(NOMINATIM_URL + "/status.php", timeout=TIMEOUT)
        if r.status_code == 200:
            return {"phase": "ready", "detail": "Import abgeschlossen – bereit"}
        if r.status_code == 503:
            body = r.text.strip()[:200]
            return {"phase": "importing", "detail": body or "Import läuft…"}
        return {"phase": "unknown", "detail": f"HTTP {r.status_code}"}
    except requests.exceptions.ConnectionError:
        state = _docker_state("nominatim-nrw")
        detail = "Container läuft, HTTP noch nicht erreichbar" if state == "running" else f"Container: {state}"
        phase  = "starting" if state == "running" else state
        return {"phase": phase, "detail": detail}
    except Exception as e:
        return {"phase": "error", "detail": str(e)}


def _build_status() -> dict:
    services = []
    for svc in SERVICES:
        url = svc["url"]() if callable(svc["url"]) else svc["url"]
        ok, code = _http_ok(url)
        docker = _docker_state(svc["container"])
        entry = {"key": svc["key"], "name": svc["name"],
                 "http_ok": ok, "http_code": code, "docker": docker}
        if svc["key"] == "nominatim":
            entry["nominatim"] = _nominatim_progress()
        services.append(entry)
    return {"services": services}


# ---------------------------------------------------------------------------
# Path helpers  (alles relativ zu OSM_DATA_ROOT)
# ---------------------------------------------------------------------------

def _paths() -> dict:
    data = Path(OSM_DATA_ROOT)
    return {
        # Docker-Stack (immer im Repo)
        "docker_dir":    DOCKER_DIR,
        "compose_yml":   DOCKER_DIR / "docker-compose.yml",
        "env_link":      DOCKER_DIR / ".env",
        "ors_config":    DOCKER_DIR / "ors" / "ors-docker" / "config" / "ors-config.yml",
        "vroom_conf":    DOCKER_DIR / "vroom" / "config.yml",
        "gh_config":     DOCKER_DIR / "graphhopper" / "config.yml",
        "ts_config":     DOCKER_DIR / "tileserver" / "config.json",
        "ts_styles":     DOCKER_DIR / "tileserver" / "styles" / "osm-bright",
        # Laufzeit-Daten (auf dem Daten-Laufwerk)
        "osm_import":    data / "osm-import",
        "nominatim_pbf": data / "osm-import" / "nrw.osm.pbf",
        "nominatim_data":data / "nominatim-data",
        "overpass_data": data / "overpass-data",
        "gh_data":       data / "graphhopper",
        "gh_pbf":        data / "graphhopper" / "nordrhein-westfalen-latest.osm.pbf",
        "ors_data":      data / "ors",
        "ors_pbf":       data / "ors" / "files" / "osm_file.pbf",
        "mbtiles_dir":   data / "mbtiles",
        "mbtiles":       data / "mbtiles" / "germany.mbtiles",
    }


def _finfo(path) -> dict:
    p = Path(path)
    if p.exists():
        try:
            size = p.stat().st_size
            return {"ok": True, "path": str(p), "size_mb": round(size / 1024 ** 2, 1),
                    "symlink": p.is_symlink()}
        except Exception:
            return {"ok": True, "path": str(p), "size_mb": None, "symlink": False}
    return {"ok": False, "path": str(p)}


def _setup_state() -> dict:
    p = _paths()

    docker_bin = shutil.which("docker")
    if docker_bin:
        try:
            r = subprocess.run(["docker", "ps"], capture_output=True, timeout=5)
            if r.returncode == 0:
                docker_ok = True
            elif b"permission denied" in r.stderr:
                docker_ok = "no-permission"
            elif b"no such file" in r.stderr or b"cannot connect" in r.stderr:
                docker_ok = "no-daemon"
            else:
                docker_ok = "error"
        except Exception:
            docker_ok = "error"
    else:
        docker_ok = False

    try:
        r = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=5)
        dc_ok  = r.returncode == 0
        dc_ver = r.stdout.strip()[:80] if dc_ok else ""
    except Exception:
        dc_ok, dc_ver = False, ""

    return {
        "osm_data_root":   OSM_DATA_ROOT,
        "docker":          {"ok": docker_ok, "bin": docker_bin},
        "docker_compose":  {"ok": dc_ok, "version": dc_ver},
        "compose_yml":     _finfo(p["compose_yml"]),
        "ors_config":      _finfo(p["ors_config"]),
        "vroom_config":    _finfo(p["vroom_conf"]),
        "gh_config":       _finfo(p["gh_config"]),
        "ts_styles":       {"ok": (p["ts_styles"] / "style.json").exists(), "path": str(p["ts_styles"])},
        "nominatim_pbf":   _finfo(p["nominatim_pbf"]),
        "ors_pbf":         _finfo(p["ors_pbf"]),
        "graphhopper_pbf": _finfo(p["gh_pbf"]),
        "mbtiles":         _finfo(p["mbtiles"]),
        "data_dirs": {
            "osm_import":     p["osm_import"].exists(),
            "nominatim_data": p["nominatim_data"].exists(),
            "overpass_data":  p["overpass_data"].exists(),
            "gh_data":        p["gh_data"].exists(),
            "ors_data":       p["ors_data"].exists(),
            "mbtiles_dir":    p["mbtiles_dir"].exists(),
        },
        "geofabrik_url":   GEOFABRIK_PBF_URL,
        "mbtiles_url":     MBTILES_URL,
    }


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _log(msg: str) -> str:
    return _sse({"t": "log", "msg": msg})


def _progress(done: int, total: int) -> str:
    pct = round(done / total * 100, 1) if total else 0
    return _sse({"t": "progress", "pct": pct,
                 "done_mb": round(done / 1024 ** 2, 1),
                 "total_mb": round(total / 1024 ** 2, 1)})


def _done(ok: bool, msg: str = "") -> str:
    return _sse({"t": "done", "ok": ok, "msg": msg})


def _run_compose(args: list[str]):
    compose_yml = DOCKER_DIR / "docker-compose.yml"
    env_file    = CONFIG_DIR / "osm.env"
    cmd = ["docker", "compose",
           "-f", str(compose_yml),
           "--env-file", str(env_file)] + args
    yield _log("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        for line in proc.stdout:
            yield _log(line.rstrip())
        proc.wait()
        ok = proc.returncode == 0
        yield _done(ok, "" if ok else f"Exit-Code {proc.returncode}")
    except FileNotFoundError:
        yield _done(False, "docker nicht gefunden")
    except Exception as e:
        yield _done(False, str(e))


# ---------------------------------------------------------------------------
# Setup step executors
# ---------------------------------------------------------------------------

def _step_create_dirs():
    p = _paths()
    dirs = [
        p["osm_import"],
        p["nominatim_data"],
        p["overpass_data"],
        p["gh_data"],
        p["ors_data"] / "files",
        p["ors_data"] / "graphs",
        p["ors_data"] / "config",
        p["ors_data"] / "logs",
        p["mbtiles_dir"],
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        yield _log(f"✓ {d}")

    # docker/.env als Symlink auf ../config/osm.env damit manuelles
    # 'docker compose' im docker/-Verzeichnis direkt funktioniert
    env_link   = p["env_link"]
    env_target = CONFIG_DIR / "osm.env"
    if not env_link.exists():
        try:
            env_link.symlink_to(env_target)
            yield _log(f"✓ docker/.env → {env_target}")
        except Exception as e:
            yield _log(f"WARN: docker/.env Symlink fehlgeschlagen: {e}")

    yield _done(True, "Verzeichnisse bereit")


def _step_download_pbf():
    p = _paths()
    pbf = p["nominatim_pbf"]
    tmp = pbf.with_suffix(".pbf.tmp")

    yield _log(f"Ziel:   {pbf}")
    yield _log(f"Quelle: {GEOFABRIK_PBF_URL}")

    if pbf.exists():
        size_mb = round(pbf.stat().st_size / 1024 ** 2, 1)
        yield _log(f"Datei existiert bereits ({size_mb} MB) – übersprungen.")
        yield _done(True, "PBF bereits vorhanden")
        return

    pbf.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(GEOFABRIK_PBF_URL, stream=True, timeout=60,
                          headers={"User-Agent": "Joormann-OSM-Lab/1.0"}) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            if total:
                yield _log(f"Dateigröße: {round(total / 1024 ** 2, 1)} MB")
            downloaded = 0
            last_pct = -1
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=512 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        pct = int(downloaded / total * 100) if total else 0
                        if pct != last_pct:
                            last_pct = pct
                            yield _progress(downloaded, total)
        tmp.rename(pbf)
        yield _log(f"✓ Download abgeschlossen: {pbf}")
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        yield _done(False, f"Download fehlgeschlagen: {e}")
        return

    # Symlinks für GraphHopper und ORS auf die eine PBF-Datei
    for link in [p["gh_pbf"], p["ors_pbf"]]:
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            try:
                link.symlink_to(pbf.resolve())
                yield _log(f"✓ Symlink: {link}")
            except Exception as e:
                shutil.copy2(str(pbf), str(link))
                yield _log(f"✓ Kopiert (kein Symlink möglich): {link} ({e})")
        else:
            yield _log(f"→ Bereits vorhanden: {link}")

    yield _done(True, "PBF heruntergeladen")


def _step_download_mbtiles():
    p = _paths()
    mbt = p["mbtiles"]

    if not MBTILES_URL:
        yield _done(False, "MBTILES_URL nicht gesetzt – bitte in config/osm.env eintragen")
        return

    yield _log(f"Ziel:   {mbt}")
    yield _log(f"Quelle: {MBTILES_URL}")

    if mbt.exists():
        yield _log(f"Datei existiert bereits ({round(mbt.stat().st_size / 1024**2, 1)} MB) – übersprungen.")
        yield _done(True, "MBTiles bereits vorhanden")
        return

    mbt.parent.mkdir(parents=True, exist_ok=True)
    tmp = mbt.with_suffix(".mbtiles.tmp")
    try:
        with requests.get(MBTILES_URL, stream=True, timeout=60,
                          headers={"User-Agent": "Joormann-OSM-Lab/1.0"}) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            if total:
                yield _log(f"Dateigröße: {round(total / 1024**2, 1)} MB")
            downloaded = 0
            last_pct = -1
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        pct = int(downloaded / total * 100) if total else 0
                        if pct != last_pct:
                            last_pct = pct
                            yield _progress(downloaded, total)
        tmp.rename(mbt)
        yield _log(f"✓ Download abgeschlossen: {mbt}")
        yield _done(True, "MBTiles heruntergeladen")
    except Exception as e:
        if tmp.exists():
            tmp.unlink()
        yield _done(False, f"Download fehlgeschlagen: {e}")


def _step_check_configs():
    p = _paths()
    all_ok = True
    checks = [
        (p["compose_yml"], "docker-compose.yml"),
        (p["ors_config"],  "ORS ors-config.yml"),
        (p["vroom_conf"],  "Vroom config.yml"),
        (p["gh_config"],   "GraphHopper config.yml"),
        (p["ts_config"],   "TileServer config.json"),
    ]
    for path, label in checks:
        if path.exists():
            yield _log(f"✓ {label}: {path}")
        else:
            yield _log(f"FEHLER: {label} fehlt: {path}")
            all_ok = False

    style_json = p["ts_styles"] / "style.json"
    if style_json.exists():
        yield _log(f"✓ TileServer osm-bright/style.json: {style_json}")
    else:
        yield _log(f"WARN: {style_json} fehlt – Tiles-Style muss manuell in docker/tileserver/styles/ abgelegt werden")

    yield _done(all_ok, "" if all_ok else "Einige Konfigurationsdateien fehlen – Repository vollständig geklont?")


def _step_install_docker():
    yield _log("Erkenne Betriebssystem…")
    try:
        with open("/etc/os-release") as f:
            yield _log(f.read().strip()[:200])
    except Exception:
        yield _log("WARNUNG: /etc/os-release nicht lesbar")

    yield _log("")
    yield _log("Lade Docker-Installationsskript von get.docker.com …")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", "curl -fsSL https://get.docker.com | sh"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in proc.stdout:
            yield _log(line.rstrip())
        proc.wait()
        ok = proc.returncode == 0
        if ok:
            user = os.environ.get("SUDO_USER") or os.environ.get("USER", "")
            if user:
                subprocess.run(["usermod", "-aG", "docker", user], capture_output=True)
                yield _log(f"✓ Benutzer '{user}' zur docker-Gruppe hinzugefügt (Re-Login erforderlich)")
        yield _done(ok, "" if ok else f"Exit-Code {proc.returncode}")
    except Exception as e:
        yield _done(False, str(e))


def _step_docker_logs():
    compose_yml = DOCKER_DIR / "docker-compose.yml"
    env_file    = CONFIG_DIR / "osm.env"
    cmd = ["docker", "compose",
           "-f", str(compose_yml),
           "--env-file", str(env_file),
           "logs", "--tail=200", "--no-log-prefix"]
    yield _log("$ " + " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        for line in (result.stdout + result.stderr).splitlines():
            yield _log(line)
        yield _done(True)
    except Exception as e:
        yield _done(False, str(e))


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


@app.route("/setup")
def setup_page():
    return render_template("setup.html")


@app.route("/api/status")
def api_status():
    return jsonify(_build_status())


@app.route("/api/setup/state")
def api_setup_state():
    return jsonify(_setup_state())


@app.route("/api/setup/save-config", methods=["POST"])
def api_setup_save_config():
    global OSM_DATA_ROOT, MBTILES_URL, GEOFABRIK_PBF_URL

    data = request.get_json(force=True, silent=True) or {}
    new_root      = (data.get("osm_data_root") or "").strip()
    new_mbtiles   = (data.get("mbtiles_url") or "").strip()
    new_geofabrik = (data.get("geofabrik_url") or "").strip()

    if not new_root:
        return jsonify({"error": "Kein Pfad angegeben"}), 400

    env_file = CONFIG_DIR / "osm.env"
    content  = env_file.read_text()

    def _set(text: str, key: str, value: str) -> str:
        pattern = rf"^{re.escape(key)}=.*$"
        replacement = f"{key}={value}"
        if re.search(pattern, text, re.MULTILINE):
            return re.sub(pattern, replacement, text, flags=re.MULTILINE)
        return text + f"\n{replacement}\n"

    content = _set(content, "OSM_DATA_ROOT", new_root)
    if new_mbtiles:
        content = _set(content, "MBTILES_URL", new_mbtiles)
    if new_geofabrik:
        content = _set(content, "GEOFABRIK_PBF_URL", new_geofabrik)

    env_file.write_text(content)

    OSM_DATA_ROOT     = new_root
    if new_mbtiles:
        MBTILES_URL   = new_mbtiles
    if new_geofabrik:
        GEOFABRIK_PBF_URL = new_geofabrik

    return jsonify({"ok": True, "osm_data_root": new_root})


_STEPS = {
    "create-dirs":      _step_create_dirs,
    "check-configs":    _step_check_configs,
    "download-pbf":     _step_download_pbf,
    "download-mbtiles": _step_download_mbtiles,
    "install-docker":   _step_install_docker,
    "docker-pull":      lambda: _run_compose(["pull"]),
    "docker-start":     lambda: _run_compose(["up", "-d"]),
    "docker-stop":      lambda: _run_compose(["down"]),
    "docker-restart":   lambda: _run_compose(["restart"]),
    "docker-logs":      _step_docker_logs,
}


@app.route("/api/setup/run/<step>")
def api_setup_run(step: str):
    if step not in _STEPS:
        return jsonify({"error": "Unbekannter Schritt"}), 400

    def generate():
        try:
            yield from _STEPS[step]()
        except Exception as e:
            yield _done(False, str(e))

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/geocode", methods=["POST"])
def api_geocode():
    data  = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Kein Suchbegriff angegeben"}), 400

    params = {"q": query, "format": "jsonv2", "addressdetails": 1,
              "limit": int(data.get("limit", 10)), "accept-language": "de"}
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

    params = {"lat": lat, "lon": lon, "format": "jsonv2", "addressdetails": 1,
              "zoom": int(data.get("zoom", 18)), "accept-language": "de"}
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
