# Home Streaming Audio Janitor (HSAJ)

HSAJ is split into two parts:
- `bridge/` is a Node.js bridge that connects to Roon, exposes HTTP endpoints, and broadcasts transport events over WebSocket.
- `core/` is a Python engine that scans the library, stores metadata in SQLite, builds previewable plans, applies quarantine/Atmos actions, manages retention, and serves an operator API/UI.

## Development setup

Requirements:
- Node.js 18+
- Python 3.11+
- `npm`

Install dependencies:
```bash
cd bridge
npm install
cd ../core
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Run the bridge:
```bash
cd bridge
npm run dev
```

Run the core listener:
```bash
cd core
source .venv/bin/activate
python -m core.app
# or
hsaj listen --config configs/hsaj.yaml
```

## Bridge behavior

The bridge exposes:
- `GET /health`
- `GET /track/{id}` for observed tracks
- `GET /blocked`, backed by `BRIDGE_BLOCKED_FILE` or `BRIDGE_BLOCKED_JSON`
- WebSocket `ws://127.0.0.1:8080/events` for `transport_event` payloads

`GET /health` now also reports:
- `contract_version`
- `blocked_source`
- `security`

Operational bridge probes:
- `GET /live`
- `GET /ready`
- `GET /metrics`

Observed transport events are sourced from Roon transport subscriptions. Demo tracks and demo blocks are no longer used.
`/blocked` accepts `artist`, `album`, and `track` objects and preserves metadata like `artist`, `album`, `title`, `track_number`, and `duration_ms` for the core inheritance flow.

## Security defaults

Bridge defaults:
- `BRIDGE_HOST=127.0.0.1`
- `BRIDGE_PORT=8080`
- `BRIDGE_WS_PATH=/events`

Optional hardening:
- set `BRIDGE_SHARED_SECRET` to require `X-HSAJ-Token` on HTTP and `?token=` or the same header on WebSocket
- set `HSAJ_BRIDGE_TOKEN` in the core environment so HTTP and WS clients authenticate automatically
- set `BRIDGE_BLOCKED_FILE=/path/to/blocked.json` or `BRIDGE_BLOCKED_JSON='[...]'` so `GET /blocked` returns a real blocked-object feed

Do not expose the bridge publicly without a shared secret.

## Core workflows

Common commands:
- `hsaj scan --config configs/hsaj.yaml`
- `hsaj roon sync --config configs/hsaj.yaml`
- `hsaj roon sync --config configs/hsaj.yaml --cache-tracks`
- `hsaj plan --config configs/hsaj.yaml`
- `hsaj apply --config configs/hsaj.yaml --dry-run`
- `hsaj cleanup --config configs/hsaj.yaml`
- `hsaj restore <file_id-or-path> --config configs/hsaj.yaml`
- `hsaj serve --config configs/hsaj.yaml`
- `hsaj exempt list --config configs/hsaj.yaml`
- `hsaj exempt add-file 123 --config configs/hsaj.yaml`
- `hsaj exempt add-artist "Artist Name" --config configs/hsaj.yaml`
- `hsaj exempt add-album "Artist Name" "Album Name" --config configs/hsaj.yaml`

`hsaj roon sync --cache-tracks` now closes the loop for track blocks by fetching `/track/{id}` and warming `RoonItemCache` before planning.

`hsaj apply --dry-run` now records the full serialized plan in `actions_log` with `action="plan"`, plus a `dry_run` marker.

`hsaj cleanup` applies quarantine retention policy:
- if `policy.auto_delete=false`, expired candidates are marked as `expired`
- if `policy.auto_delete=true`, due quarantine files are physically deleted and logged

`hsaj serve` starts the operator API and a thin built-in UI. The core exposes:
- `GET /`
- `GET /live`
- `GET /health`
- `GET /ready`
- `GET /metrics`
- `GET /plan`
- `POST /apply`
- `POST /restore`
- `POST /cleanup`
- `GET /candidates`
- `GET /soft-candidates`
- `GET /reviews`
- `POST /soft-review-preview`
- `POST /soft-review-action`
- `GET /actions`
- `GET /stats`
- `GET /exemptions`
- `POST /exemptions`
- `DELETE /exemptions/{id}`

If `security.operator_token` or `HSAJ_OPERATOR_TOKEN` is set, operator routes require
`X-HSAJ-Operator-Token`. Health, live, ready, and metrics remain available for probes.

Soft candidates are advisory only. Operators can:
- dismiss them, which suppresses the same advisory signal for that file
- convert them into a file-level exemption
- create a preview quarantine plan and then apply it explicitly

## CI

GitHub Actions runs:
- `npm ci && npm test` in `bridge`
- formatting and tests in `core`

## Systemd

Systemd service files live under `configs/systemd/`. See [docs/systemd.md](docs/systemd.md).

## License

MIT
