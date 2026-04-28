#!/usr/bin/env bash
# Konvertiert nrw.osm.pbf -> nrw.osm.bz2 (für Overpass-Import).
# Nutzt osmconvert + pbzip2 (parallel) — schnell, ~5-10 min für NRW.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$PROJECT_ROOT/config"
LOG_DIR="$PROJECT_ROOT/runtime/logs"
LOG_FILE="$LOG_DIR/convert-pbf-to-bz2.log"
mkdir -p "$LOG_DIR"

OSM_DATA_ROOT="$(grep -E '^OSM_DATA_ROOT=' "$CONFIG_DIR/osm.env" | cut -d= -f2-)"
[[ -z "$OSM_DATA_ROOT" ]] && { echo "FEHLER: OSM_DATA_ROOT nicht in osm.env."; exit 1; }

SRC="$OSM_DATA_ROOT/osm-import/nrw.osm.pbf"
DST="$OSM_DATA_ROOT/osm-import/nrw.osm.bz2"
TMP="$DST.tmp"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

log "=== PBF → OSM-XML.bz2 Konvertierung ==="
log "Quelle: $SRC"
log "Ziel:   $DST"

[[ ! -f "$SRC" ]] && { log "FEHLER: $SRC nicht da."; exit 1; }

if [[ -f "$DST" ]]; then
  EXISTING_MB=$(($(stat -c '%s' "$DST") / 1024 / 1024))
  log "Ziel existiert bereits ($EXISTING_MB MB) — übersprungen."
  log "Re-Konvertierung gewünscht? Erst: rm \"$DST\""
  exit 0
fi

# Tools sicherstellen
need_install=()
command -v osmconvert >/dev/null || need_install+=(osmctools)
command -v pbzip2     >/dev/null || need_install+=(pbzip2)

if (( ${#need_install[@]} > 0 )); then
  log "Installiere benötigte Tools: ${need_install[*]}"
  log "(Du wirst nach dem sudo-Passwort gefragt.)"
  sudo apt-get update -qq | tee -a "$LOG_FILE"
  sudo apt-get install -y "${need_install[@]}" 2>&1 | tee -a "$LOG_FILE"
fi

# Schreibrecht im osm-import/ sicherstellen — Nominatim-Container hat das
# Verzeichnis evtl. mit Container-User-UID angelegt (z.B. 1001).
DST_DIR="$(dirname "$DST")"
if [[ ! -w "$DST_DIR" ]]; then
  log "── Ziel-Verzeichnis nicht schreibbar — Owner anpassen ──"
  log "  sudo chown $USER:$USER $DST_DIR"
  sudo chown "$USER:$USER" "$DST_DIR" | tee -a "$LOG_FILE"
fi

CORES=$(nproc)
SRC_MB=$(($(stat -c '%s' "$SRC") / 1024 / 1024))
log ""
log "Quelle: $SRC_MB MB | Cores für pbzip2: $CORES"
log "── Starte Pipeline: osmconvert (PBF→XML) | pbzip2 -p$CORES ──"
log "Dauert ~5-10 min auf moderner CPU."
log ""

START=$(date +%s)
osmconvert "$SRC" 2>>"$LOG_FILE" \
  | pbzip2 -c -p"$CORES" 2>>"$LOG_FILE" \
  > "$TMP"

if [[ ! -s "$TMP" ]]; then
  log "FEHLER: Output ist leer."
  rm -f "$TMP"
  exit 1
fi

mv "$TMP" "$DST"
END=$(date +%s)
DST_MB=$(($(stat -c '%s' "$DST") / 1024 / 1024))
log "✓ Fertig in $((END-START))s — $DST ($DST_MB MB)"
log ""
log "Nächster Schritt: Overpass-Container starten:"
log "  cd $PROJECT_ROOT"
log "  docker compose -f docker/docker-compose.yml --env-file config/osm.env up -d overpass"
