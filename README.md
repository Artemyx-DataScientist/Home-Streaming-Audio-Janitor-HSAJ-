# Home Streaming Audio Janitor (HSAJ)

HSAJ is split into two parts:
- `bridge/` is a Node.js bridge that connects to Roon, exposes HTTP endpoints, and broadcasts transport events over WebSocket.
- `core/` is a Python engine that scans the library, stores metadata in SQLite, builds plans, and applies quarantine/Atmos actions.

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
- `hsaj restore <file_id-or-path> --config configs/hsaj.yaml`

`hsaj roon sync --cache-tracks` now closes the loop for track blocks by fetching `/track/{id}` and warming `RoonItemCache` before planning.

`hsaj apply --dry-run` now records the full serialized plan in `actions_log` with `action="plan"`, plus a `dry_run` marker.

## CI

GitHub Actions runs:
- `npm ci && npm test` in `bridge`
- formatting and tests in `core`

## Systemd

Systemd service files live under `configs/systemd/`. See [docs/systemd.md](docs/systemd.md).

## License

MIT
