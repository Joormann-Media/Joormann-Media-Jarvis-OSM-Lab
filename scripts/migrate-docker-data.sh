#!/usr/bin/env bash
# scripts/migrate-docker-data.sh
# Verschiebt Docker-Daten und OSM-Import nach OSM_DATA_ROOT (aus config/osm.env)
# Ausführen: sudo bash scripts/migrate-docker-data.sh [Zielpfad]
# Beispiel:  sudo bash scripts/migrate-docker-data.sh /mnt/data_toshiba

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/config/osm.env"

# Zielpfad: Argument oder aus osm.env
if [[ -n "${1:-}" ]]; then
    TARGET="$1"
else
    TARGET=$(grep -E '^OSM_DATA_ROOT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d '"' | xargs)
    TARGET="${TARGET:-/mnt/data_toshiba}"
fi

DOCKER_DATA_ROOT="$TARGET/docker"
OSM_IMPORT_SRC="/mnt/HDD3/nominatim-nrw/import"
OSM_IMPORT_DST="$TARGET/osm-import"
DAEMON_JSON="/etc/docker/daemon.json"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[FEHLER]${NC} $*" >&2; exit 1; }
step() { echo -e "\n${YELLOW}=== $* ===${NC}"; }

[[ $EUID -eq 0 ]] || die "Bitte als root ausführen: sudo bash $0 [Zielpfad]"
mountpoint -q "$TARGET" || die "$TARGET ist kein Einhängepunkt – Laufwerk gemountet?"

echo "Ziel-Laufwerk: $TARGET"
df -h "$TARGET"

# ── Docker-Compose Stack stoppen ─────────────────────────────────────────────
step "Docker-Stack stoppen"
if command -v docker &>/dev/null && docker ps &>/dev/null 2>&1; then
    docker compose \
        -f "$REPO_DIR/docker/docker-compose.yml" \
        --env-file "$ENV_FILE" \
        down 2>/dev/null || warn "compose down fehlgeschlagen"
fi

# ── Docker-Daemon stoppen ────────────────────────────────────────────────────
step "Docker-Daemon stoppen"
systemctl stop docker.socket docker 2>/dev/null || true
sleep 2
systemctl is-active --quiet docker && die "Docker läuft noch"
ok "Docker gestoppt"

# ── OSM-Import-Daten von HDD3 ────────────────────────────────────────────────
step "OSM-Importdaten: $OSM_IMPORT_SRC → $OSM_IMPORT_DST"
if [[ -d "$OSM_IMPORT_SRC" ]]; then
    mkdir -p "$OSM_IMPORT_DST"
    rsync -aHAX --info=progress2 "$OSM_IMPORT_SRC/" "$OSM_IMPORT_DST/"
    ok "OSM-Importdaten kopiert"
    ls -lh "$OSM_IMPORT_DST/"
else
    warn "$OSM_IMPORT_SRC nicht gefunden – überspringe"
fi

# ── Docker-Daten verschieben ─────────────────────────────────────────────────
step "Docker-Daten: /var/lib/docker → $DOCKER_DATA_ROOT"
if [[ -d /var/lib/docker && ! -L /var/lib/docker ]]; then
    if [[ -d "$DOCKER_DATA_ROOT" ]]; then
        warn "$DOCKER_DATA_ROOT existiert bereits – überspringe rsync"
    else
        echo "Kopiere /var/lib/docker → $DOCKER_DATA_ROOT …"
        rsync -aHAX --info=progress2 /var/lib/docker/ "$DOCKER_DATA_ROOT/"
        ok "Docker-Daten kopiert"
    fi
fi

# ── daemon.json ──────────────────────────────────────────────────────────────
step "daemon.json schreiben"
cat > "$DAEMON_JSON" <<EOF
{
  "storage-driver": "fuse-overlayfs",
  "data-root": "$DOCKER_DATA_ROOT"
}
EOF
ok "daemon.json: $(cat $DAEMON_JSON)"

# ── altes /var/lib/docker sichern ────────────────────────────────────────────
step "Altes /var/lib/docker sichern"
if [[ -d /var/lib/docker && ! -L /var/lib/docker ]]; then
    mv /var/lib/docker /var/lib/docker.bak
    ok "Gesichert als /var/lib/docker.bak"
    df -h /
fi

# ── Docker neu starten ───────────────────────────────────────────────────────
step "Docker-Daemon neu starten"
systemctl daemon-reload
systemctl reset-failed docker 2>/dev/null || true
systemctl start docker
sleep 3
systemctl is-active --quiet docker || { journalctl -u docker -n 40 --no-pager; die "Docker startet nicht"; }
ok "Docker läuft (data-root: $DOCKER_DATA_ROOT)"
docker info | grep -E "Docker Root Dir|Storage Driver" || true

# ── osm.env aktualisieren ────────────────────────────────────────────────────
step "config/osm.env aktualisieren"
cp "$ENV_FILE" "$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"
if grep -q '^OSM_DATA_ROOT=' "$ENV_FILE"; then
    sed -i "s|^OSM_DATA_ROOT=.*|OSM_DATA_ROOT=$TARGET|" "$ENV_FILE"
else
    echo "OSM_DATA_ROOT=$TARGET" >> "$ENV_FILE"
fi
ok "OSM_DATA_ROOT=$TARGET gesetzt"

# ── Stack neu starten ─────────────────────────────────────────────────────────
step "Docker-Stack starten"
docker compose \
    -f "$REPO_DIR/docker/docker-compose.yml" \
    --env-file "$ENV_FILE" \
    up -d
sleep 5
docker compose -f "$REPO_DIR/docker/docker-compose.yml" --env-file "$ENV_FILE" ps

# ── Abschluss ─────────────────────────────────────────────────────────────────
step "Migration abgeschlossen"
ok "Docker data-root:  $DOCKER_DATA_ROOT"
ok "OSM-Importdaten:  $OSM_IMPORT_DST"
echo ""
echo "Backup entfernen wenn alles läuft:"
echo "  sudo rm -rf /var/lib/docker.bak"
echo "  sudo rm -rf /mnt/HDD3/nominatim-nrw  # HDD3 freigeben"
