import json
import os
import shutil
import subprocess
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"

for env_file in (CONFIG_DIR / "ports.env", CONFIG_DIR / "osm.env", BASE_DIR / ".env"):
    if env_file.exists():
        load_dotenv(env_file)

FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5079"))
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

NOMINATIM_URL      = os.environ.get("NOMINATIM_URL",      "http://localhost:7071").rstrip("/")
ORS_URL            = os.environ.get("ORS_URL",            "http://localhost:8082").rstrip("/")
GRAPHHOPPER_URL    = os.environ.get("GRAPHHOPPER_URL",    "http://localhost:8989").rstrip("/")
TILESERVER_URL     = os.environ.get("TILESERVER_URL",     "http://localhost:8083").rstrip("/")
VROOM_URL          = os.environ.get("VROOM_URL",          "http://localhost:8084").rstrip("/")
OSM_COMPOSE_DIR    = os.environ.get("OSM_COMPOSE_DIR",    "/home/djanebmb/osm-neu")
NOMINATIM_PBF_PATH = os.environ.get("NOMINATIM_PBF_PATH", "/mnt/HDD3/nominatim-nrw/import/nrw.osm.pbf")
NOMINATIM_DATA_DIR = os.environ.get("NOMINATIM_DATA_DIR", "/mnt/HDD3/nominatim-nrw/import")
GEOFABRIK_PBF_URL  = os.environ.get("GEOFABRIK_PBF_URL",
    "https://download.geofabrik.de/europe/germany/nordrhein-westfalen-latest.osm.pbf")
MBTILES_URL        = os.environ.get("MBTILES_URL", "")

TIMEOUT = 3

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

SERVICES = [
    {"key": "nominatim",   "name": "Nominatim (Geocoding)", "url": NOMINATIM_URL   + "/status",      "container": "nominatim-nrw"},
    {"key": "ors",         "name": "OpenRouteService",       "url": ORS_URL         + "/ors/v2/health","container": "ors-app"},
    {"key": "graphhopper", "name": "GraphHopper",            "url": GRAPHHOPPER_URL + "/health",       "container": "graphhopper"},
    {"key": "tileserver",  "name": "TileServer GL",          "url": TILESERVER_URL  + "/health",       "container": "tileserver"},
    {"key": "vroom",       "name": "VROOM",                  "url": VROOM_URL       + "/",             "container": "vroom"},
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
        entry = {"key": svc["key"], "name": svc["name"],
                 "http_ok": ok, "http_code": code, "docker": docker}
        if svc["key"] == "nominatim":
            entry["nominatim"] = _nominatim_progress()
        services.append(entry)
    return {"services": services}


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def _paths() -> dict:
    d = Path(OSM_COMPOSE_DIR)
    return {
        "compose_dir":      d,
        "compose_yml":      d / "docker-compose.yml",
        "nominatim_import": Path(NOMINATIM_DATA_DIR),
        "nominatim_pbf":    Path(NOMINATIM_PBF_PATH),
        "ors_files":        d / "ors" / "ors-docker" / "files",
        "ors_pbf":          d / "ors" / "ors-docker" / "files" / "osm_file.pbf",
        "ors_config_dir":   d / "ors" / "ors-docker" / "config",
        "ors_config":       d / "ors" / "ors-docker" / "config" / "ors-config.yml",
        "ors_graphs":       d / "ors" / "ors-docker" / "graphs",
        "gh_data":          d / "graphhopper" / "data",
        "gh_pbf":           d / "graphhopper" / "data" / "nordrhein-westfalen-latest.osm.pbf",
        "ts_data":          d / "tileserver",
        "ts_config":        d / "tileserver" / "config.json",
        "ts_styles":        d / "tileserver" / "styles" / "osm-bright",
        "mbtiles":          d / "tileserver" / "germany.mbtiles",
        "vroom_conf":       d / "vroom" / "config.yml",
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

    # Docker: binary vorhanden + Daemon erreichbar
    docker_bin = shutil.which("docker")
    if docker_bin:
        try:
            r = subprocess.run(["docker", "ps"], capture_output=True, timeout=5)
            if r.returncode == 0:
                docker_ok = True
            elif b"permission denied" in r.stderr:
                docker_ok = "no-permission"   # binary + daemon, aber Benutzer fehlt in docker-Gruppe
            elif b"no such file" in r.stderr or b"cannot connect" in r.stderr:
                docker_ok = "no-daemon"       # binary da, daemon nicht gestartet
            else:
                docker_ok = "error"
        except Exception:
            docker_ok = "error"
    else:
        docker_ok = False

    # Docker Compose plugin
    try:
        r = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=5)
        dc_ok = r.returncode == 0
        dc_ver = r.stdout.strip()[:80] if dc_ok else ""
    except Exception:
        dc_ok, dc_ver = False, ""

    return {
        "docker":            {"ok": docker_ok, "bin": docker_bin},
        "docker_compose":    {"ok": dc_ok, "version": dc_ver},
        "compose_dir":       {"ok": p["compose_dir"].exists(),      "path": str(p["compose_dir"])},
        "compose_yml":       _finfo(p["compose_yml"]),
        "nominatim_dir":     {"ok": p["nominatim_import"].exists(), "path": str(p["nominatim_import"])},
        "nominatim_pbf":     _finfo(p["nominatim_pbf"]),
        "ors_pbf":           _finfo(p["ors_pbf"]),
        "graphhopper_pbf":   _finfo(p["gh_pbf"]),
        "mbtiles":           _finfo(p["mbtiles"]),
        "ts_styles":         {"ok": (p["ts_styles"] / "style.json").exists(), "path": str(p["ts_styles"])},
        "ors_config":        _finfo(p["ors_config"]),
        "vroom_config":      _finfo(p["vroom_conf"]),
        "geofabrik_url":     GEOFABRIK_PBF_URL,
        "mbtiles_url":       MBTILES_URL,
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
    p = _paths()
    cmd = ["docker", "compose", "-f", str(p["compose_yml"])] + args
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
        p["nominatim_import"],
        p["compose_dir"],
        p["ors_files"],
        p["ors_config_dir"],
        p["ors_graphs"],
        p["gh_data"],
        p["ts_data"],
        p["vroom_conf"].parent,
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        yield _log(f"✓ {d}")
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

    # Symlinks für ORS und GraphHopper
    for link in [p["ors_pbf"], p["gh_pbf"]]:
        if not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
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


def _step_generate_configs():
    p = _paths()

    # ORS config
    if not p["ors_config"].exists():
        p["ors_config_dir"].mkdir(parents=True, exist_ok=True)
        p["ors_config"].write_text(_ors_config_template())
        yield _log(f"✓ ORS-Config erstellt: {p['ors_config']}")
    else:
        yield _log(f"→ ORS-Config vorhanden: {p['ors_config']}")

    # TileServer config.json
    if not p["ts_config"].exists():
        p["ts_data"].mkdir(parents=True, exist_ok=True)
        p["ts_config"].write_text(json.dumps({
            "options": {"paths": {"root": "/data", "styles": "styles", "fonts": "fonts"}},
            "data": {"germany": {"mbtiles": "germany.mbtiles"}},
            "styles": {"osm-bright": {"style": "osm-bright/style.json"}},
        }, indent=2))
        yield _log(f"✓ TileServer config.json erstellt: {p['ts_config']}")
    else:
        yield _log(f"→ TileServer config.json vorhanden")

    # Vroom config
    if not p["vroom_conf"].exists():
        p["vroom_conf"].parent.mkdir(parents=True, exist_ok=True)
        p["vroom_conf"].write_text(_vroom_config_template())
        yield _log(f"✓ Vroom config.yml erstellt: {p['vroom_conf']}")
    else:
        yield _log(f"→ Vroom config.yml vorhanden")

    # docker-compose.yml
    if not p["compose_yml"].exists():
        p["compose_dir"].mkdir(parents=True, exist_ok=True)
        p["compose_yml"].write_text(_compose_template())
        yield _log(f"✓ docker-compose.yml erstellt: {p['compose_yml']}")
    else:
        yield _log(f"→ docker-compose.yml vorhanden: {p['compose_yml']}")

    yield _done(True, "Konfigurationen bereit")


def _step_install_docker():
    # Prüft OS und führt das offizielle Install-Script aus
    yield _log("Erkenne Betriebssystem…")
    try:
        with open("/etc/os-release") as f:
            os_info = f.read()
        yield _log(os_info.strip()[:200])
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
            # Benutzer zur docker-Gruppe hinzufügen
            user = os.environ.get("SUDO_USER") or os.environ.get("USER", "")
            if user:
                subprocess.run(["usermod", "-aG", "docker", user], capture_output=True)
                yield _log(f"✓ Benutzer '{user}' zur docker-Gruppe hinzugefügt (Re-Login erforderlich)")
        yield _done(ok, "" if ok else f"Exit-Code {proc.returncode}")
    except Exception as e:
        yield _done(False, str(e))


def _step_docker_logs():
    p = _paths()
    cmd = ["docker", "compose", "-f", str(p["compose_yml"]),
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
# Config templates
# ---------------------------------------------------------------------------

def _ors_config_template() -> str:
    return f"""ors:
  engine:
    source_file: /home/ors/files/osm_file.pbf
    graphs_root_path: /home/ors/graphs
    profiles:
      car:
        enabled: true
        profile: driving-car
        preparation:
          min_network_size: 200
          methods:
            ch:
              enabled: true
              threads: 1
              weightings: fastest

  services:
    geocoding:
      enabled: true
      providers:
        nominatim:
          provider: nominatim
          priority: 1
          url: http://nominatim-nrw:8080/
          timeout: 2000
          minBatchSize: 1

  request_limits:
    max_locations: 200
    max_matrix_locations: 200
    max_visited_nodes: 10000000
"""


def _vroom_config_template() -> str:
    return """cliArgs:
  geometry: false
  planmode: false
  threads: 4
  explore: 5
  limit: '1mb'
  logdir: '/..'
  logsize: '100M'
  maxlocations: 1000
  maxvehicles: 200
  override: true
  path: ''
  port: 3000
  router: 'ors'
  timeout: 300000
  baseurl: '/'

routingServers:
  ors:
    driving-car:
      host: 'ors'
      port: '8082'
      baseurl: '/ors/v2'
"""


def _compose_template() -> str:
    d = Path(OSM_COMPOSE_DIR)
    nom_import = NOMINATIM_DATA_DIR
    return f"""services:
  tileserver:
    image: klokantech/tileserver-gl
    container_name: tileserver
    ports:
      - "8083:80"
    volumes:
      - {d}/tileserver:/data:ro
    command: ["--config", "/data/config.json", "--no-cors"]
    restart: unless-stopped
    networks: [tiles-net]

  nominatim-nrw:
    image: mediagis/nominatim:4.5
    container_name: nominatim-nrw
    ports:
      - "7071:8080"
    volumes:
      - nominatim-data:/var/lib/postgresql/16/main
      - {nom_import}:/nominatim
    environment:
      POSTGRES_PASSWORD: nominatim
      NOMINATIM_PASSWORD: nominatim
      POSTGRES_DB: nominatim
      PBF_PATH: /nominatim/nrw.osm.pbf
      NOMINATIM_IMPORT: "1"
      NOMINATIM_THREADS: "4"
    shm_size: "1gb"
    restart: unless-stopped
    networks: [tiles-net]

  ors:
    image: openrouteservice/openrouteservice:v8.0.0
    container_name: ors-app
    ports:
      - "8082:8082"
    volumes:
      - {d}/ors/ors-docker:/home/ors
      - {d}/ors/ors-docker/config/ors-config.yml:/home/ors/config/ors-config.yml:ro
    environment:
      ORS_CONFIG_LOCATION: /home/ors/config/ors-config.yml
      XMS: 8g
      XMX: 12g
    depends_on:
      - nominatim-nrw
    restart: unless-stopped
    networks: [tiles-net]

  graphhopper:
    image: israelhikingmap/graphhopper:latest
    container_name: graphhopper
    ports:
      - "8989:8989"
    volumes:
      - {d}/graphhopper/data:/data
    environment:
      JAVA_OPTS: -Xms2g -Xmx8g
    command: ["--input","/data/nordrhein-westfalen-latest.osm.pbf",
              "--graph-cache","/data/default-gh",
              "--host","0.0.0.0"]
    restart: unless-stopped
    networks: [tiles-net]

  vroom:
    image: vroomvrp/vroom-docker:v1.10.0
    container_name: vroom
    ports:
      - "8084:3000"
    volumes:
      - {d}/vroom:/conf
    environment:
      VROOM_ROUTER: ors
    depends_on:
      - ors
    restart: unless-stopped
    networks: [tiles-net]

networks:
  tiles-net:
    driver: bridge

volumes:
  nominatim-data:
"""


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


_STEPS = {
    "create-dirs":     _step_create_dirs,
    "download-pbf":    _step_download_pbf,
    "download-mbtiles":_step_download_mbtiles,
    "gen-configs":     _step_generate_configs,
    "install-docker":  _step_install_docker,
    "docker-pull":     lambda: _run_compose(["pull"]),
    "docker-start":    lambda: _run_compose(["up", "-d"]),
    "docker-stop":     lambda: _run_compose(["down"]),
    "docker-restart":  lambda: _run_compose(["restart"]),
    "docker-logs":     _step_docker_logs,
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
    data = request.get_json(force=True, silent=True) or {}
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
