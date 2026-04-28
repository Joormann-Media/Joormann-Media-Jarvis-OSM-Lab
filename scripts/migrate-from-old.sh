#!/usr/bin/env bash
# Migration vom alten OSM-Stack (joormann-media.local) auf diesen Host.
# Holt MBTiles, Fonts und Styles und aktiviert sie im Tileserver.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Quelle (GVFS-SFTP-Mount, weil bereits authentifiziert) ────────────
SRC="/run/user/1000/gvfs/sftp:host=joormann-media.local,user=djanebmb/home/djanebmb/osm-neu"

# ── Ziel-Pfade ─────────────────────────────────────────────────────────
CONFIG_DIR="$PROJECT_ROOT/config"
DOCKER_DIR="$PROJECT_ROOT/docker"
LOG_DIR="$PROJECT_ROOT/runtime/logs"
LOG_FILE="$LOG_DIR/migrate-from-old.log"

mkdir -p "$LOG_DIR"

# OSM_DATA_ROOT aus osm.env lesen
OSM_DATA_ROOT="$(grep -E '^OSM_DATA_ROOT=' "$CONFIG_DIR/osm.env" | cut -d= -f2-)"
if [[ -z "$OSM_DATA_ROOT" ]]; then
  echo "FEHLER: OSM_DATA_ROOT nicht in $CONFIG_DIR/osm.env gefunden." >&2
  exit 1
fi

DEST_MBTILES="$OSM_DATA_ROOT/mbtiles"
DEST_FONTS="$DOCKER_DIR/tileserver/fonts"
DEST_STYLES="$DOCKER_DIR/tileserver/styles"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

# Wrapper: docker-Befehle laufen über `sg docker` falls Shell keine
# docker-Gruppe geladen hat (z.B. wenn Script aus altem Shell-Tree gestartet).
if id -nG | tr ' ' '\n' | grep -qx docker; then
  _docker() { docker "$@"; }
else
  _docker() { sg docker -c "docker $(printf '%q ' "$@")"; }
fi

log "=== Migration gestartet ==="
log "Quelle: $SRC"
log "Ziel:   $OSM_DATA_ROOT (mbtiles)"
log "Ziel:   $DOCKER_DIR/tileserver (fonts, styles)"
log ""

# ── 1) Quelle prüfen ──────────────────────────────────────────────────
if [[ ! -d "$SRC" ]]; then
  log "FEHLER: GVFS-Mount nicht erreichbar unter $SRC"
  log "Im Datei-Manager (Nautilus) den SFTP-Connect zu joormann-media.local einmal öffnen."
  exit 1
fi

if [[ ! -f "$SRC/tileserver/germany.mbtiles" ]]; then
  log "FEHLER: $SRC/tileserver/germany.mbtiles existiert nicht."
  exit 1
fi

SRC_SIZE=$(stat -c '%s' "$SRC/tileserver/germany.mbtiles")
SRC_MB=$((SRC_SIZE / 1024 / 1024))
log "Quelldatei: germany.mbtiles ($SRC_MB MB / $SRC_SIZE Bytes)"

# ── 2) Tileserver stoppen ─────────────────────────────────────────────
log ""
log "── Stoppe tileserver-Container (falls läuft) ──"
if _docker ps --format '{{.Names}}' | grep -qx tileserver; then
  _docker compose -f "$DOCKER_DIR/docker-compose.yml" \
                  --env-file "$CONFIG_DIR/osm.env" \
                  stop tileserver 2>&1 | tee -a "$LOG_FILE"
else
  log "(tileserver nicht aktiv)"
fi

# ── 3) MBTiles übertragen ─────────────────────────────────────────────
log ""
log "── Kopiere germany.mbtiles ($SRC_MB MB) — das dauert ──"
mkdir -p "$DEST_MBTILES"

# Alte Dummy-/Reste löschen
if [[ -e "$DEST_MBTILES/germany.mbtiles" ]] && [[ ! -d "$DEST_MBTILES/germany.mbtiles" ]]; then
  EXISTING_SIZE=$(stat -c '%s' "$DEST_MBTILES/germany.mbtiles" 2>/dev/null || echo 0)
  if [[ "$EXISTING_SIZE" -lt "$SRC_SIZE" ]]; then
    log "Bestehende Datei ($EXISTING_SIZE Bytes) zu klein/leer → ersetzen"
    rm -f "$DEST_MBTILES/germany.mbtiles"
  fi
fi

# rsync mit Fortschritt
rsync -aHP --info=progress2 \
      "$SRC/tileserver/germany.mbtiles" \
      "$DEST_MBTILES/germany.mbtiles" 2>&1 | tee -a "$LOG_FILE"

# Verifizieren
DEST_SIZE=$(stat -c '%s' "$DEST_MBTILES/germany.mbtiles")
if [[ "$DEST_SIZE" -ne "$SRC_SIZE" ]]; then
  log "FEHLER: Größe stimmt nicht (src=$SRC_SIZE, dest=$DEST_SIZE)"
  exit 1
fi
log "✓ MBTiles übertragen ($DEST_SIZE Bytes)"

# ── 4) Fonts ───────────────────────────────────────────────────────────
log ""
log "── Kopiere fonts/ ──"
if [[ -d "$SRC/tileserver/fonts" ]]; then
  mkdir -p "$DEST_FONTS"
  rsync -aHP "$SRC/tileserver/fonts/" "$DEST_FONTS/" 2>&1 | tee -a "$LOG_FILE"
  log "✓ Fonts übertragen ($(find "$DEST_FONTS" -type f | wc -l) Dateien)"
else
  log "(keine fonts/ am Quellhost)"
fi

# ── 5) Styles (nur Custom-Erkenntnis, vorhandenes osm-bright nicht überschreiben) ──
log ""
log "── Prüfe styles/ auf Custom-Styles ──"
if [[ -d "$SRC/tileserver/styles" ]]; then
  for style_dir in "$SRC/tileserver/styles"/*/; do
    [[ -d "$style_dir" ]] || continue
    name=$(basename "$style_dir")
    if [[ -d "$DEST_STYLES/$name" ]]; then
      log "  → $name: bereits im Repo, übersprungen"
    else
      log "  → $name: kopiere (war nicht im Repo)"
      rsync -aHP "$style_dir" "$DEST_STYLES/$name/" 2>&1 | tee -a "$LOG_FILE"
    fi
  done
else
  log "(keine styles/ am Quellhost)"
fi

# ── 6) tileserver/config.json reaktivieren ────────────────────────────
log ""
log "── Aktiviere germany-Eintrag in config.json ──"
CONFIG_JSON="$DOCKER_DIR/tileserver/config.json"
python3 - "$CONFIG_JSON" <<'PY' | tee -a "$LOG_FILE"
import json, sys
p = sys.argv[1]
with open(p) as f:
    cfg = json.load(f)
cfg.setdefault("data", {})
cfg["data"]["germany"] = {"mbtiles": "germany.mbtiles"}
with open(p, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"✓ {p} aktualisiert")
PY

# ── 7) Tileserver wieder starten ──────────────────────────────────────
log ""
log "── Starte tileserver neu ──"
_docker compose -f "$DOCKER_DIR/docker-compose.yml" \
                --env-file "$CONFIG_DIR/osm.env" \
                up -d tileserver 2>&1 | tee -a "$LOG_FILE"

sleep 4
log ""
log "── Tileserver-Logs (letzte 15 Zeilen) ──"
_docker logs tileserver --tail 15 2>&1 | tee -a "$LOG_FILE"

log ""
log "=== Migration abgeschlossen ==="
log "Tileserver-URL: http://localhost:8083"
