# Weather-Lab Geo Bridge

Diese Bridge stellt stabile, read-only Geo-Endpunkte fuer ein separates Weather-Lab bereit. Sie nutzt standardmaessig Nominatim, normalisiert die Antworten und cached identische Anfragen dateibasiert.

## Endpunkte fuer das Weather-Lab

- `GET /api/geo/geocode?q=...`  
  Ort oder Adresse in `lat/lon` aufloesen.

- `GET /api/geo/reverse?lat=...&lon=...`  
  Koordinate in Adresse, Ort, Stadtteil und Land aufloesen.

- `GET /api/geo/resolve-location?q=...`  
  Alias fuer die robuste Weather-Lab-Ortsaufloesung. Fuer Weather-Lab bevorzugt diesen Endpunkt verwenden, wenn nur ein Freitext-Ort vorliegt.

- `GET /api/geo/health`  
  Healthcheck fuer Provider, Cache und Timeout-Konfiguration.

- `GET /api/geo/capabilities`  
  Maschinenlesbare Capabilities, Endpunkte und vorbereitete Permission Keys.

Bestehende Endpunkte wie `POST /api/geocode` und `POST /api/reverse` bleiben unveraendert fuer UI und bestehende Integrationen nutzbar.

## Beispiel-Requests

```bash
curl 'http://localhost:5079/api/geo/geocode?q=Duisburg'
```

```bash
curl 'http://localhost:5079/api/geo/reverse?lat=51.4344&lon=6.7623'
```

```bash
curl 'http://localhost:5079/api/geo/resolve-location?q=Kasslerfeld%20Duisburg'
```

```bash
curl 'http://localhost:5079/api/geo/health'
```

```bash
curl 'http://localhost:5079/api/geo/capabilities'
```

## Beispiel-Response

```json
{
  "ok": true,
  "query": "Duisburg",
  "location": {
    "label": "Duisburg, Nordrhein-Westfalen, Deutschland",
    "lat": 51.4344,
    "lon": 6.7623,
    "city": "Duisburg",
    "district": "",
    "country": "Deutschland",
    "postcode": ""
  },
  "source": "nominatim",
  "cached": false
}
```

Bei Cache-Treffern bleibt das Schema identisch, nur `source` und `cached` aendern sich:

```json
{
  "ok": true,
  "query": "Duisburg",
  "location": {
    "label": "Duisburg, Nordrhein-Westfalen, Deutschland",
    "lat": 51.4344,
    "lon": 6.7623,
    "city": "Duisburg",
    "district": "",
    "country": "Deutschland",
    "postcode": ""
  },
  "source": "local_cache",
  "cached": true
}
```

## Fehlerantworten

Keine Treffer:

```json
{
  "ok": false,
  "error": "no_results",
  "message": "Keine Treffer gefunden.",
  "query": "UnbekannterOrtXYZ",
  "source": "nominatim"
}
```

Ungueltige Koordinaten:

```json
{
  "ok": false,
  "error": "invalid_coordinates",
  "message": "lat muss zwischen -90 und 90 liegen"
}
```

Provider nicht erreichbar:

```json
{
  "ok": false,
  "error": "geo_service_unreachable",
  "message": "Nominatim/Geo-Service nicht erreichbar.",
  "query": "Duisburg",
  "source": "nominatim"
}
```

Timeout:

```json
{
  "ok": false,
  "error": "geo_timeout",
  "message": "Geo-Service Timeout.",
  "query": "Duisburg",
  "source": "nominatim"
}
```

## ENV-Konfiguration

Beispielwerte stehen in `.env.dist`:

```env
GEO_DEFAULT_PROVIDER=nominatim
GEO_NOMINATIM_BASE_URL=http://localhost:7071
GEO_CACHE_TTL_SECONDS=86400
GEO_TIMEOUT_SECONDS=5
```

Optionale Erweiterung:

```env
GEO_CACHE_PATH=/home/djanebmb/projects/Joormann-Media-Jarvis-OSM-Lab/runtime/cache/geo-cache.json
GEO_USER_AGENT=Joormann-OSM-Lab/1.0
```

Die Bridge liest zuerst `config/ports.env`, dann `config/osm.env`, dann `.env`. Harte URLs im Weather-Lab sollten vermieden werden: das Weather-Lab soll die Base-URL des OSM-Labs aus seiner eigenen Config lesen und dann diese `/api/geo/*`-Pfade verwenden.

## Cache-Verhalten

- Cache-Typ: einfacher Dateicache unter `runtime/cache/geo-cache.json`.
- TTL: `GEO_CACHE_TTL_SECONDS`, Standard `86400` Sekunden.
- Deaktivieren: `GEO_CACHE_TTL_SECONDS=0`.
- Identische Geocode- und Reverse-Anfragen werden aus dem Cache beantwortet, solange die TTL gueltig ist.
- Cache-Dateien liegen unter `runtime/cache/` und werden nicht versioniert.

## Fallback-Verhalten

- Primaerprovider ist `nominatim`.
- `GEO_NOMINATIM_BASE_URL` kann auf lokale Nominatim-Instanzen oder einen kompatiblen internen Geo-Service zeigen.
- Bei nicht erreichbarem Provider liefert die Bridge konsistentes JSON mit `ok=false` und HTTP `503`.
- Bei Timeout liefert die Bridge konsistentes JSON mit `ok=false` und HTTP `504`.
- Bei fehlenden Treffern liefert die Bridge HTTP `404` mit `error=no_results`.
- Es werden keine Secrets benoetigt und keine mutierenden Aktionen ausgefuehrt.

## MCP / Permissions

Im aktuellen OSM-Lab wurde keine eigene MCP-Server-Implementierung gefunden. Die Geo-Bridge veroeffentlicht aber vorbereitete read-only Actions und Permission Keys ueber `/api/geo/capabilities` und das Service-Manifest:

- `geo.geocode` mit `geo.resolve`
- `geo.reverse` mit `geo.read`
- `geo.resolve_location` mit `geo.resolve`
- `geo.health` mit `geo.health`

Alle Actions sind read-only.

## Manuelle Testliste

1. Service starten: `./scripts/start-dev.sh` oder bestehende Systemd/Docker-Startprozedur nutzen.
2. Health pruefen: `curl 'http://localhost:5079/api/geo/health'`.
3. Ort aufloesen: `curl 'http://localhost:5079/api/geo/geocode?q=Duisburg'`.
4. Dieselbe Anfrage erneut senden und pruefen, dass `cached=true` und `source=local_cache` kommt.
5. Reverse pruefen: `curl 'http://localhost:5079/api/geo/reverse?lat=51.4344&lon=6.7623'`.
6. Ungueltige Koordinaten pruefen: `curl 'http://localhost:5079/api/geo/reverse?lat=999&lon=6.7623'`.
7. Keine Treffer pruefen: `curl 'http://localhost:5079/api/geo/geocode?q=UnbekannterOrtXYZ123'`.
8. Capabilities pruefen: `curl 'http://localhost:5079/api/geo/capabilities'`.
9. Bestehende API pruefen: `curl -X POST 'http://localhost:5079/api/geocode' -H 'Content-Type: application/json' -d '{"query":"Duisburg","limit":1}'`.
