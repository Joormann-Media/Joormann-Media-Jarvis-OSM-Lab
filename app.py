import json
import os
import re
import shutil
import socket
import platform
import subprocess
import time
import uuid
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

BASE_DIR   = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
DOCKER_DIR = BASE_DIR / "docker"
RUNTIME_DIR = BASE_DIR / "runtime"
PORTAL_CONFIG_PATH = RUNTIME_DIR / "portal-config.json"


def _load_env():
    for env_file in (CONFIG_DIR / "ports.env", CONFIG_DIR / "osm.env", BASE_DIR / ".env"):
        if env_file.exists():
            load_dotenv(env_file, override=True)


_load_env()

FLASK_HOST  = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT  = int(os.environ.get("FLASK_PORT", "5079"))
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"
API_CORS_ALLOW_ORIGIN = os.environ.get("API_CORS_ALLOW_ORIGIN", "*").strip() or "*"

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
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


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


def _get_json_with_retry(url: str, *, params: dict | None = None,
                         timeout: int | float = TIMEOUT,
                         headers: dict | None = None,
                         attempts: int = 3) -> tuple[int, object]:
    last_status = 0
    last_error = None
    for i in range(max(1, attempts)):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=headers)
            last_status = r.status_code
            if r.status_code >= 500 and i + 1 < attempts:
                time.sleep(0.25 * (i + 1))
                continue
            return r.status_code, r.json()
        except requests.exceptions.JSONDecodeError as e:
            last_error = e
            break
        except Exception as e:
            last_error = e
            if i + 1 < attempts:
                time.sleep(0.25 * (i + 1))
                continue
    if last_error:
        raise last_error
    return last_status, {}


def _post_json_with_retry(url: str, *, data: dict | None = None,
                          timeout: int | float = 30,
                          headers: dict | None = None,
                          attempts: int = 2) -> tuple[int, object]:
    last_status = 0
    last_error = None
    for i in range(max(1, attempts)):
        try:
            r = requests.post(url, data=data, timeout=timeout, headers=headers)
            last_status = r.status_code
            if r.status_code >= 500 and i + 1 < attempts:
                time.sleep(0.25 * (i + 1))
                continue
            return r.status_code, r.json()
        except requests.exceptions.JSONDecodeError as e:
            last_error = e
            break
        except Exception as e:
            last_error = e
            if i + 1 < attempts:
                time.sleep(0.25 * (i + 1))
                continue
    if last_error:
        raise last_error
    return last_status, {}


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


def _service_manifest() -> dict:
    base = request.url_root.rstrip("/")
    return {
        "service": {
            "name": "Joormann Media Jarvis OSM Lab",
            "slug": "jarvis-osm-lab",
            "version": "2026.04",
            "runtime": "flask",
        },
        "node": {
            "hostname": os.environ.get("HOSTNAME", ""),
            "data_root": OSM_DATA_ROOT,
        },
        "endpoints": {
            "ui": base + "/",
            "info": base + "/info",
            "health": base + "/health",
            "api_catalog": base + "/api",
            "status": base + "/api/status",
            "manifest": base + "/api/service-manifest",
            "capabilities": base + "/api/capabilities",
            "geocode": base + "/api/geocode",
            "reverse": base + "/api/reverse",
            "route": base + "/api/route",
            "poi": base + "/api/poi",
            "poi_categories": base + "/api/poi/categories",
            "route_ui": base + "/route",
            "map_ui": base + "/",
        },
        "capabilities": [
            "geo.geocode",
            "geo.reverse_geocode",
            "geo.address_search",
            "geo.address_autocomplete",
            "map.route_plan",
            "map.route_plan.multistop",
            "map.route_plan.profile_car",
            "map.route_plan.profile_foot",
            "map.route_plan.profile_bike",
            "map.poi_search",
            "map.poi_categories",
            "map.tiles.vector",
            "jarvis.routing.osm_lab",
            "jarvis.routing.address_lookup",
            "jarvis.routing.route_planning",
            "jarvis.routing.poi_lookup",
        ],
        "routing": {
            "profiles": ["car", "foot", "bike"],
            "poi_categories": list(_POI_CATEGORIES.keys()) if "_POI_CATEGORIES" in globals() else [],
            "intents": [
                "address_lookup",
                "route_planning",
                "poi_lookup",
            ],
            "entrypoints": {
                "address_lookup": "/api/geocode",
                "route_planning": "/api/route",
                "poi_lookup": "/api/poi",
                "reverse_lookup": "/api/reverse",
            },
        },
        "integration": {
            "family_panel_ready": True,
            "cors_allow_origin": API_CORS_ALLOW_ORIGIN,
            "auth": "none",
            "notes": [
                "POST /api/geocode mit {query, limit}",
                "POST /api/reverse mit {lat, lon, zoom?}",
                "POST /api/route mit {profile, points:[[lat,lon], ...]}",
                "POST /api/poi mit {category, bbox:[south,west,north,east]}",
                "GET /api liefert Endpoint-Katalog inkl. Kurzbeschreibung",
            ],
        },
    }


def _api_catalog_description(method: str, path: str) -> str:
    key = (method.upper().strip(), path.strip())
    descriptions: Dict[tuple[str, str], str] = {
        ("GET", "/api"): "Endpoint-Katalog des OSM-Labs.",
        ("GET", "/api/status"): "Live-Status aller OSM-Services.",
        ("GET", "/api/service-manifest"): "Node-Selbstbeschreibung inkl. Routing-Infos.",
        ("GET", "/api/capabilities"): "Kurzfassung von Capabilities + Endpoints.",
        ("GET", "/api/setup/state"): "Setup-Zustand (Docker, Datenpfade, Dateien).",
        ("GET", "/api/setup/browse-dirs"): "Verzeichnis-Browser fuer Setup.",
        ("POST", "/api/setup/create-dir"): "Neues Verzeichnis im Setup anlegen.",
        ("POST", "/api/setup/save-config"): "OSM-Pfade/URLs in config/osm.env speichern.",
        ("POST", "/api/geocode"): "Addresssuche ueber Nominatim (+ POI-Fallback).",
        ("POST", "/api/reverse"): "Reverse-Geocoding fuer lat/lon.",
        ("POST", "/api/route"): "Routing ueber GraphHopper (car/foot/bike).",
        ("POST", "/api/poi"): "POI-Suche ueber Overpass in einer BBox.",
        ("GET", "/api/poi/categories"): "Verfuegbare POI-Kategorien.",
        ("GET", "/api/portal/status"): "Status der Jarvis-Node-Registrierung.",
        ("POST", "/api/portal/register"): "OSM-Lab am Family-Panel registrieren.",
        ("POST", "/api/portal/sync"): "Capabilities/Endpoints ans Portal synchronisieren.",
    }
    return descriptions.get(key, "")


def _api_catalog_endpoints() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for rule in app.url_map.iter_rules():
        path = str(rule.rule or "").strip()
        if not path.startswith("/api"):
            continue
        methods = [m for m in sorted(rule.methods) if m not in {"HEAD"}]
        for method in methods:
            if method == "OPTIONS":
                continue
            rows.append({
                "method": method,
                "path": path,
                "desc": _api_catalog_description(method, path),
            })
    rows.sort(key=lambda row: (row["path"], row["method"]))
    return rows

def _load_portal_config() -> dict:
    if not PORTAL_CONFIG_PATH.exists():
        return {"portal": {}}
    try:
        raw = json.loads(PORTAL_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"portal": {}}
    if not isinstance(raw, dict):
        return {"portal": {}}
    portal = raw.get("portal")
    raw["portal"] = portal if isinstance(portal, dict) else {}
    return raw


def _save_portal_config(cfg: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    PORTAL_CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _portal_registered(portal: dict) -> bool:
    return bool(
        str(portal.get("url") or "").strip()
        and str(portal.get("node_uuid") or "").strip()
        and str(portal.get("client_id") or "").strip()
    )


def _mask_key(raw: str) -> str:
    token = str(raw or "").strip()
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}***{token[-4:]}"


def _get_machine_id() -> str:
    try:
        machine_id_file = Path("/etc/machine-id")
        if machine_id_file.exists():
            value = machine_id_file.read_text(encoding="utf-8").strip()
            if value:
                return value
    except Exception:
        pass
    return hashlib.sha256(f"{socket.gethostname()}:{uuid.getnode()}".encode("utf-8")).hexdigest()


def _get_local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _do_portal_sync(cfg: Optional[dict] = None) -> dict:
    config = cfg or _load_portal_config()
    portal = config.get("portal") if isinstance(config.get("portal"), dict) else {}
    portal_url = str(portal.get("url") or "").strip()
    node_uuid = str(portal.get("node_uuid") or "").strip()
    client_id = str(portal.get("client_id") or "").strip()
    api_key = str(portal.get("api_key") or "").strip()
    if not portal_url or not node_uuid or not client_id or not api_key:
        return {"ok": False, "error": "not_registered", "message": "Portal-Credentials fehlen. POST /api/portal/register zuerst."}

    manifest = _service_manifest()
    payload = {
        "nodeUuid": node_uuid,
        "clientId": client_id,
        "service": manifest.get("service", {}),
        "capabilities": manifest.get("capabilities", []),
        "routing": manifest.get("routing", {}),
        "endpoints": manifest.get("endpoints", {}),
    }

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.post(
            f"{portal_url.rstrip('/')}/api/jarvis/node/sync",
            json=payload,
            headers=headers,
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:
        return {"ok": False, "error": "portal_unreachable", "message": f"Portal nicht erreichbar: {exc}"}

    if not data.get("ok"):
        return {
            "ok": False,
            "error": "sync_failed",
            "message": data.get("message", "Sync fehlgeschlagen."),
            "status": resp.status_code,
            "response": data,
        }
    return {"ok": True, "status": resp.status_code, "response": data}


@app.after_request
def add_cors_headers(response):
    if request.path.startswith("/api/") or request.path == "/health":
        response.headers["Access-Control-Allow-Origin"] = API_CORS_ALLOW_ORIGIN
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


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

    # Hardlinks für GraphHopper und ORS auf die eine PBF-Datei.
    # Hardlinks (statt Symlinks) damit die Datei in Docker-Containern korrekt
    # erscheint — Symlinks würden im Container ins Leere zeigen, weil ihr
    # Zielpfad außerhalb des Bind-Mounts liegt.
    for link in [p["gh_pbf"], p["ors_pbf"]]:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or (link.exists() and link.stat().st_ino != pbf.stat().st_ino):
            link.unlink()
        if not link.exists():
            try:
                os.link(pbf, link)
                yield _log(f"✓ Hardlink: {link}")
            except OSError as e:
                shutil.copy2(str(pbf), str(link))
                yield _log(f"✓ Kopiert (Hardlink fehlgeschlagen): {link} ({e})")
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

@app.route("/info")
def info():
    manifest = _service_manifest()
    examples = [
        {
            "title": "Adresse suchen",
            "method": "POST",
            "path": "/api/geocode",
            "json": {"query": "Kölner Dom", "limit": 5},
        },
        {
            "title": "Koordinate in Adresse auflösen",
            "method": "POST",
            "path": "/api/reverse",
            "json": {"lat": 50.9413, "lon": 6.9583, "zoom": 18},
        },
        {
            "title": "Route mit mehreren Punkten",
            "method": "POST",
            "path": "/api/route",
            "json": {
                "profile": "car",
                "points": [
                    [50.9413, 6.9583],
                    [51.2277, 6.7735],
                ],
            },
        },
        {
            "title": "POIs in Bounding Box suchen",
            "method": "POST",
            "path": "/api/poi",
            "json": {"category": "pharmacy", "bbox": [50.90, 6.90, 50.99, 7.05]},
        },
    ]
    return render_template(
        "info.html",
        manifest=manifest,
        endpoints=_api_catalog_endpoints(),
        examples=examples,
    )


@app.route("/route")
def route_page():
    return render_template("route.html",
                           tileserver_url=TILESERVER_URL)


@app.route("/status")
def status_page():
    return render_template("status.html")


@app.route("/setup")
def setup_page():
    return render_template("setup.html")


@app.route("/health")
def health():
    status = _build_status()
    services = status.get("services", [])
    healthy = sum(1 for svc in services if svc.get("http_ok"))
    overall = "healthy" if healthy == len(services) and services else "degraded"
    return jsonify({
        "status": overall,
        "service": "jarvis-osm-lab",
        "healthy_services": healthy,
        "total_services": len(services),
        "services": services,
    })


@app.route("/api/status")
def api_status():
    return jsonify(_build_status())


@app.route("/api/capabilities")
def api_capabilities():
    manifest = _service_manifest()
    return jsonify({
        "service": manifest["service"],
        "capabilities": manifest["capabilities"],
        "routing": manifest["routing"],
        "endpoints": manifest["endpoints"],
    })


@app.route("/api/service-manifest")
def api_service_manifest():
    return jsonify(_service_manifest())

@app.get("/api")
def api_catalog():
    return jsonify(ok=True, endpoints=_api_catalog_endpoints())

@app.get("/api/portal/status")
def api_portal_status():
    cfg = _load_portal_config()
    portal = cfg.get("portal") or {}
    return jsonify(
        ok=True,
        registered=_portal_registered(portal),
        portalUrl=portal.get("url") or None,
        nodeUuid=portal.get("node_uuid") or None,
        nodeSlug=portal.get("node_slug") or None,
        machineId=_get_machine_id(),
        clientId=portal.get("client_id") or None,
        apiKeyMasked=_mask_key(portal.get("api_key") or ""),
    )


@app.post("/api/portal/register")
def api_portal_register():
    body = request.get_json(silent=True) or {}
    cfg = _load_portal_config()
    portal = cfg.get("portal") if isinstance(cfg.get("portal"), dict) else {}

    portal_url = str(body.get("portal_url") or portal.get("url") or "").strip()
    registration_token = str(body.get("registration_token") or body.get("token") or "").strip()
    if not portal_url:
        return jsonify(ok=False, error="portal_url_missing", message="Feld 'portal_url' ist erforderlich."), 400
    if not registration_token:
        return jsonify(ok=False, error="token_missing", message="Feld 'registration_token' ist erforderlich."), 400

    local_ip = _get_local_ip()
    hostname = socket.gethostname()
    fp_seed = f"{hostname}:{uuid.getnode()}:{BASE_DIR}"
    fp_hash = hashlib.sha256(fp_seed.encode("utf-8")).hexdigest()
    reg_payload: Dict[str, Any] = {
        "registrationToken": registration_token,
        "nodeName": str(body.get("node_name") or f"OSM-Lab ({hostname})"),
        "hostname": hostname,
        "type": "server",
        "os": platform.system().lower() or "linux",
        "platform": f"python-flask/{platform.python_version()}",
        "version": _service_manifest()["service"]["version"],
        "localIp": local_ip,
        "apiBaseUrl": f"http://{local_ip}:{FLASK_PORT}",
        "localUrl": f"http://{local_ip}:{FLASK_PORT}",
        "fingerprintHash": fp_hash,
        "fingerprintVersion": "1",
        "capabilities": _service_manifest()["capabilities"],
        "description": "Jarvis OSM-Lab — Geocoding, Routing, POI",
        "machineId": _get_machine_id(),
    }
    if str(portal.get("node_uuid") or "").strip():
        reg_payload["nodeUuid"] = str(portal.get("node_uuid")).strip()

    try:
        resp = requests.post(
            f"{portal_url.rstrip('/')}/api/jarvis/node/register",
            json=reg_payload,
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:
        return jsonify(ok=False, error="portal_unreachable", message=f"Portal nicht erreichbar: {exc}"), 502

    if not data.get("ok"):
        return jsonify(
            ok=False,
            error="registration_failed",
            message=data.get("message", "Registrierung fehlgeschlagen."),
            detail=data,
        ), resp.status_code

    node_data = (data.get("data") or {}).get("node") or {}
    auth_data = (data.get("data") or {}).get("auth") or {}
    portal["url"] = portal_url
    portal["client_id"] = str(auth_data.get("clientId") or portal.get("client_id") or "")
    portal["api_key"] = str(auth_data.get("apiKey") or portal.get("api_key") or "")
    portal["node_uuid"] = str(node_data.get("uuid") or portal.get("node_uuid") or "")
    portal["node_slug"] = str(node_data.get("slug") or portal.get("node_slug") or "")
    cfg["portal"] = portal
    _save_portal_config(cfg)

    sync_result = _do_portal_sync(cfg)
    return jsonify(
        ok=True,
        registered=True,
        created=bool((data.get("data") or {}).get("created")),
        node=node_data,
        auth={
            "clientId": portal.get("client_id"),
            "apiKeyPrefix": auth_data.get("apiKeyPrefix"),
            "apiKeyMasked": auth_data.get("apiKeyMasked"),
        },
        sync=sync_result,
    ), (201 if bool((data.get("data") or {}).get("created")) else 200)


@app.post("/api/portal/sync")
def api_portal_sync():
    result = _do_portal_sync()
    if not result.get("ok"):
        return jsonify(
            ok=False,
            error=result.get("error", "sync_failed"),
            message=result.get("message", ""),
        ), 502
    return jsonify(ok=True, sync=result.get("response", {}))


@app.route("/link", methods=["GET", "POST"])
def link_portal():
    cfg = _load_portal_config()
    portal = cfg.get("portal") or {}
    form = {
        "portal_url": str(portal.get("url") or "").strip(),
        "registration_token": "",
        "node_name": "",
    }
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    if request.method == "POST":
        form["portal_url"] = str(request.form.get("portal_url") or "").strip()
        form["registration_token"] = str(request.form.get("registration_token") or "").strip()
        form["node_name"] = str(request.form.get("node_name") or "").strip()

        if not form["portal_url"]:
            error = "Portal-URL fehlt."
        elif not form["registration_token"]:
            error = "Registrierungs-Token fehlt."
        else:
            payload: Dict[str, Any] = {
                "portal_url": form["portal_url"],
                "registration_token": form["registration_token"],
            }
            if form["node_name"]:
                payload["node_name"] = form["node_name"]

            with app.test_request_context("/api/portal/register", method="POST", json=payload):
                response = api_portal_register()
            if isinstance(response, tuple):
                flask_response, status_code = response
            else:
                flask_response, status_code = response, response.status_code
            data = flask_response.get_json(silent=True) or {}
            if int(status_code) >= 400 or not data.get("ok"):
                error = str(data.get("message") or data.get("error") or f"HTTP {status_code}")
            else:
                result = data
                cfg = _load_portal_config()
                portal = cfg.get("portal") or {}
                form["portal_url"] = str(portal.get("url") or form["portal_url"]).strip()
                form["registration_token"] = ""

    return render_template(
        "link.html",
        form=form,
        result=result,
        error=error,
        portal_status={
            "registered": _portal_registered(portal),
            "portal_url": portal.get("url") or "",
            "node_uuid": portal.get("node_uuid") or "",
            "node_slug": portal.get("node_slug") or "",
            "machine_id": _get_machine_id(),
            "client_id": portal.get("client_id") or "",
            "api_key_masked": _mask_key(str(portal.get("api_key") or "")),
        },
    )


@app.get("/relink")
def relink_portal():
    return link_portal()


@app.route("/api/setup/state")
def api_setup_state():
    return jsonify(_setup_state())


@app.route("/api/setup/browse-dirs")
def api_setup_browse_dirs():
    requested   = request.args.get("path", "").strip()
    show_hidden = request.args.get("hidden", "0") == "1"

    if not requested:
        for candidate in (OSM_DATA_ROOT, "/mnt", "/media", str(Path.home()), "/"):
            if candidate and Path(candidate).is_dir():
                requested = candidate
                break

    try:
        target = Path(requested).expanduser().resolve()
    except Exception as e:
        return jsonify({"error": f"Ungültiger Pfad: {e}"}), 400

    if not target.exists():
        return jsonify({"error": f"Pfad existiert nicht: {target}"}), 404
    if not target.is_dir():
        target = target.parent

    entries = []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            try:
                if not entry.is_dir():
                    continue
                hidden = entry.name.startswith(".")
                if hidden and not show_hidden:
                    continue
                entries.append({
                    "name":    entry.name,
                    "path":    str(entry),
                    "hidden":  hidden,
                    "symlink": entry.is_symlink(),
                })
            except (PermissionError, OSError):
                continue
    except PermissionError:
        return jsonify({"error": f"Keine Berechtigung: {target}"}), 403

    disk = None
    try:
        u = shutil.disk_usage(target)
        disk = {
            "total_gb": round(u.total / 1024 ** 3, 1),
            "free_gb":  round(u.free  / 1024 ** 3, 1),
            "used_gb":  round(u.used  / 1024 ** 3, 1),
            "pct_used": round(u.used / u.total * 100, 1) if u.total else 0,
        }
    except Exception:
        pass

    shortcuts = []
    for label, path in (("Home", str(Path.home())), ("/mnt", "/mnt"),
                        ("/media", "/media"), ("/", "/")):
        if Path(path).is_dir():
            shortcuts.append({"label": label, "path": path})

    return jsonify({
        "path":      str(target),
        "parent":    str(target.parent) if target.parent != target else None,
        "entries":   entries,
        "disk":      disk,
        "shortcuts": shortcuts,
    })


@app.route("/api/setup/create-dir", methods=["POST"])
def api_setup_create_dir():
    data   = request.get_json(force=True, silent=True) or {}
    parent = (data.get("parent") or "").strip()
    name   = (data.get("name")   or "").strip()

    if not parent or not name:
        return jsonify({"error": "parent und name erforderlich"}), 400
    if "/" in name or "\\" in name or name in (".", ".."):
        return jsonify({"error": "Ungültiger Ordnername"}), 400

    try:
        target = (Path(parent).expanduser() / name).resolve()
        target.mkdir(parents=False, exist_ok=False)
        return jsonify({"ok": True, "path": str(target)})
    except FileExistsError:
        return jsonify({"error": "Ordner existiert bereits"}), 409
    except PermissionError:
        return jsonify({"error": "Keine Berechtigung"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    limit = max(1, min(25, int(data.get("limit", 10))))
    try:
        results = _nominatim_search(query, limit=limit)
        if results:
            return jsonify({"results": results, "source": "nominatim"})

        poi_category, location_query = _extract_poi_query(query)
        if poi_category and location_query:
            anchors = _nominatim_search(location_query, limit=3)
            if anchors:
                anchor = anchors[0]
                alat = float(anchor["lat"])
                alon = float(anchor["lon"])
                label, pois = _query_poi(poi_category, _poi_bbox_for_point(alat, alon))
                pois = sorted(
                    pois,
                    key=lambda item: _distance_score(alat, alon, float(item["lat"]), float(item["lon"]))
                )[:limit]
                mapped = []
                for item in pois:
                    tags = item.get("tags") or {}
                    display_parts = [
                        item.get("name") or label,
                        tags.get("addr:street") or tags.get("road"),
                        tags.get("addr:housenumber") or tags.get("housenumber"),
                        tags.get("addr:postcode") or tags.get("postcode"),
                        tags.get("addr:city") or tags.get("city") or tags.get("town"),
                    ]
                    mapped.append({
                        "place_id": item.get("id"),
                        "osm_type": item.get("type"),
                        "osm_id": item.get("id"),
                        "lat": str(item["lat"]),
                        "lon": str(item["lon"]),
                        "category": "amenity",
                        "type": poi_category,
                        "display_name": ", ".join(str(p) for p in display_parts if p),
                        "name": item.get("name") or label,
                        "address": tags,
                    })
                if mapped:
                    return jsonify({
                        "results": mapped,
                        "source": "poi-fallback",
                        "anchor": anchor,
                    })

        return jsonify({"results": [], "source": "nominatim"})
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Nominatim nicht erreichbar"}), 503
    except Exception as e:
        poi_category, location_query = _extract_poi_query(query)
        if poi_category and location_query:
            try:
                anchors = _nominatim_search(location_query, limit=3)
                if anchors:
                    anchor = anchors[0]
                    alat = float(anchor["lat"])
                    alon = float(anchor["lon"])
                    label, pois = _query_poi(poi_category, _poi_bbox_for_point(alat, alon))
                    pois = sorted(
                        pois,
                        key=lambda item: _distance_score(alat, alon, float(item["lat"]), float(item["lon"]))
                    )[:limit]
                    mapped = [{
                        "place_id": item.get("id"),
                        "osm_type": item.get("type"),
                        "osm_id": item.get("id"),
                        "lat": str(item["lat"]),
                        "lon": str(item["lon"]),
                        "category": "amenity",
                        "type": poi_category,
                        "display_name": ", ".join(str(p) for p in [
                            item.get("name") or label,
                            (item.get("tags") or {}).get("addr:street") or (item.get("tags") or {}).get("road"),
                            (item.get("tags") or {}).get("addr:housenumber") or (item.get("tags") or {}).get("housenumber"),
                            (item.get("tags") or {}).get("addr:postcode") or (item.get("tags") or {}).get("postcode"),
                            (item.get("tags") or {}).get("addr:city") or (item.get("tags") or {}).get("city") or (item.get("tags") or {}).get("town"),
                        ] if p),
                        "name": item.get("name") or label,
                        "address": item.get("tags") or {},
                    } for item in pois]
                    if mapped:
                        return jsonify({
                            "results": mapped,
                            "source": "poi-fallback-after-error",
                            "anchor": anchor,
                        })
            except Exception:
                pass
        return jsonify({"error": str(e)}), 500


@app.route("/api/route", methods=["POST"])
def api_route():
    """Multi-Point-Routing über GraphHopper. Erwartet:
       { "profile": "car|foot|bike", "points": [[lat,lon], ...] (mind. 2) }
    Antwort: { distance_m, time_ms, geometry, instructions }"""
    data    = request.get_json(force=True, silent=True) or {}
    profile = (data.get("profile") or "car").lower()
    points  = data.get("points") or []

    if profile not in ("car", "foot", "bike"):
        return jsonify({"error": "Profil muss car, foot oder bike sein"}), 400
    if not isinstance(points, list) or len(points) < 2:
        return jsonify({"error": "Mindestens zwei Wegpunkte nötig"}), 400

    params = [("profile", profile),
              ("points_encoded", "false"),
              ("instructions", "true"),
              ("locale", "de"),
              ("calc_points", "true")]
    for p in points:
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            return jsonify({"error": f"Ungültiger Wegpunkt: {p}"}), 400
        params.append(("point", f"{lat},{lon}"))

    try:
        r = requests.get(GRAPHHOPPER_URL + "/route", params=params, timeout=15)
        if r.status_code >= 400:
            return jsonify({"error": f"GraphHopper {r.status_code}: {r.text[:200]}"}), 502
        gh = r.json()
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "GraphHopper nicht erreichbar"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    paths = gh.get("paths") or []
    if not paths:
        return jsonify({"error": "Keine Route gefunden"}), 404
    p0 = paths[0]
    return jsonify({
        "distance_m":   p0.get("distance"),
        "time_ms":      p0.get("time"),
        "geometry":     p0.get("points"),       # GeoJSON LineString
        "instructions": p0.get("instructions", []),
        "bbox":         p0.get("bbox"),
        "ascend":       p0.get("ascend"),
        "descend":      p0.get("descend"),
    })


_POI_CATEGORIES = {
    "pharmacy":     ("amenity", "pharmacy",     "Apotheke"),
    "bar":          ("amenity", "bar",          "Bar"),
    "pub":          ("amenity", "pub",          "Kneipe"),
    "restaurant":   ("amenity", "restaurant",   "Restaurant"),
    "cafe":         ("amenity", "cafe",         "Café"),
    "fuel":         ("amenity", "fuel",         "Tankstelle"),
    "bank":         ("amenity", "bank",         "Bank"),
    "atm":          ("amenity", "atm",          "Geldautomat"),
    "hospital":     ("amenity", "hospital",     "Krankenhaus"),
    "doctors":      ("amenity", "doctors",      "Arzt"),
    "school":       ("amenity", "school",       "Schule"),
    "supermarket":  ("shop",    "supermarket",  "Supermarkt"),
    "bakery":       ("shop",    "bakery",       "Bäckerei"),
    "convenience":  ("shop",    "convenience",  "Kiosk"),
}

_POI_QUERY_ALIASES = {
    "apotheke": "pharmacy",
    "bar": "bar",
    "kneipe": "pub",
    "pub": "pub",
    "restaurant": "restaurant",
    "cafe": "cafe",
    "café": "cafe",
    "tankstelle": "fuel",
    "bank": "bank",
    "geldautomat": "atm",
    "krankenhaus": "hospital",
    "arzt": "doctors",
    "schule": "school",
    "supermarkt": "supermarket",
    "bäckerei": "bakery",
    "baeckerei": "bakery",
    "kiosk": "convenience",
}


def _nominatim_search(query: str, limit: int = 10) -> list[dict]:
    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": limit,
        "accept-language": "de",
    }
    status, payload = _get_json_with_retry(
        NOMINATIM_URL + "/search",
        params=params,
        timeout=max(TIMEOUT, 8),
        headers={"User-Agent": "Joormann-OSM-Lab/1.0"},
        attempts=3,
    )
    if status >= 400:
        raise requests.HTTPError(f"{status} for {query}")
    return payload if isinstance(payload, list) else []


def _extract_poi_query(query: str) -> tuple[str | None, str]:
    q = re.sub(r"\s+", " ", query.strip())
    if not q:
        return None, ""
    parts = q.split(" ", 1)
    token = parts[0].lower()
    category = _POI_QUERY_ALIASES.get(token)
    if not category:
        return None, q
    rest = parts[1].strip() if len(parts) > 1 else ""
    return category, rest


def _poi_bbox_for_point(lat: float, lon: float, radius_deg: float = 0.04) -> list[float]:
    return [lat - radius_deg, lon - radius_deg, lat + radius_deg, lon + radius_deg]


def _distance_score(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return ((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2) ** 0.5


def _query_poi(category: str, bbox: list[float]) -> tuple[str, list[dict]]:
    s, w, n, e = bbox
    key, value, label = _POI_CATEGORIES[category]
    query = (
        f"[out:json][timeout:25];"
        f"("
        f"  node[\"{key}\"=\"{value}\"]({s},{w},{n},{e});"
        f"  way[\"{key}\"=\"{value}\"]({s},{w},{n},{e});"
        f");"
        f"out center tags;"
    )
    status, payload = _post_json_with_retry(
        OVERPASS_URL + "/api/interpreter",
        data={"data": query},
        timeout=30,
        headers={"User-Agent": "Joormann-OSM-Lab/1.0"},
        attempts=2,
    )
    if status >= 400:
        raise requests.HTTPError(f"Overpass {status}")
    op = payload if isinstance(payload, dict) else {}

    pois = []
    for el in op.get("elements", []):
        if el.get("type") == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        tags = el.get("tags") or {}
        pois.append({
            "id": el.get("id"),
            "type": el.get("type"),
            "lat": lat,
            "lon": lon,
            "name": tags.get("name") or label,
            "tags": tags,
        })
    return label, pois


@app.route("/api/poi", methods=["POST"])
def api_poi():
    """POI-Suche per Overpass innerhalb einer Bbox.
       Erwartet: { "category": "<key>", "bbox": [south, west, north, east] }"""
    data     = request.get_json(force=True, silent=True) or {}
    category = (data.get("category") or "").strip()
    bbox     = data.get("bbox") or []

    if category not in _POI_CATEGORIES:
        return jsonify({"error": "Unbekannte Kategorie"}), 400
    if not isinstance(bbox, list) or len(bbox) != 4:
        return jsonify({"error": "bbox = [south, west, north, east] erwartet"}), 400
    try:
        s, w, n, e = (float(x) for x in bbox)
    except (TypeError, ValueError):
        return jsonify({"error": "bbox-Werte müssen Zahlen sein"}), 400

    # Bbox-Größe begrenzen, damit Overpass nicht überlastet
    if (n - s) * (e - w) > 4.0:
        return jsonify({"error": "Bbox zu groß — bitte mehr reinzoomen"}), 400

    try:
        label, pois = _query_poi(category, [s, w, n, e])
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Overpass nicht erreichbar (importiert evtl. noch)"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"category": category, "label": label, "results": pois})


@app.route("/api/poi/categories")
def api_poi_categories():
    return jsonify([{"key": k, "label": v[2]} for k, v in _POI_CATEGORIES.items()])


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
