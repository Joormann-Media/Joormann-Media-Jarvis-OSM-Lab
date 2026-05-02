# MCP Panel Integration

Das OSM-Lab stellt nur read-only Geo-Actions fuer Intentcenter und Routing bereit.

## Discovery

Das FamilienPanel soll diese Endpunkte pollen/syncen:

- `GET /api/manifest`
- `GET /api/capabilities`
- `GET /api/mcp/actions`
- `GET /api/mcp/settings`

Settings koennen ueber `POST /api/mcp/settings` gespeichert werden. Damit lassen sich Actions aktivieren/deaktivieren.

## Actions

| Action | Endpoint | PermissionKey |
| --- | --- | --- |
| `geo.geocode` | `GET /api/geo/geocode` | `geo.resolve` |
| `geo.reverse` | `GET /api/geo/reverse` | `geo.read` |
| `geo.resolve_location` | `GET /api/geo/resolve-location` | `geo.resolve` |
| `geo.health` | `GET /api/geo/health` | `geo.health` |
| `geo.capabilities` | `GET /api/geo/capabilities` | `geo.health` |

Alle Actions melden `read_only=true` und `requires_confirmation=false`.

## Beispiel `/api/mcp/actions`

```json
{
  "ok": true,
  "count": 5,
  "actions": [
    {
      "name": "geo.geocode",
      "endpoint": "/api/geo/geocode",
      "method": "GET",
      "enabled": true,
      "read_only": true,
      "requires_confirmation": false,
      "permission_key": "geo.resolve",
      "input_schema": {"type": "object"},
      "output_schema": {"type": "object"},
      "tags": ["geo", "geocode", "resolve", "read"]
    }
  ]
}
```

## Beispiel `/api/manifest`

```json
{
  "service": "jarvis-osm-lab",
  "name": "Jarvis OSM-Lab",
  "version": "2026.04",
  "base_url": "https://example.invalid",
  "health_url": "https://example.invalid/health",
  "capabilities_url": "https://example.invalid/api/capabilities",
  "mcp_actions_url": "https://example.invalid/api/mcp/actions",
  "actions": []
}
```

`base_url` wird aus `PUBLIC_BASE_URL` oder aus dem aktuellen Request abgeleitet.

## Curl

```bash
curl "$BASE/api/manifest"
curl "$BASE/api/capabilities"
curl "$BASE/api/mcp/actions"
curl "$BASE/api/mcp/execute?action=geo.geocode&q=Duisburg"
./scripts/check_mcp_contract.sh "$BASE"
```
