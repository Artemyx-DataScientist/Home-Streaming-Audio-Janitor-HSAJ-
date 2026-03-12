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
- `GET /blocked`, backed by live Roon browse extraction or by `BRIDGE_BLOCKED_FILE` / `BRIDGE_BLOCKED_JSON`
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
`/blocked` returns a versioned snapshot envelope with `contract_version`, `generated_at`, `source`, `item_count`, `object_types`, and `items`.
Blocked `items` accept `artist`, `album`, and `track` objects and preserve metadata like `artist`, `album`, `title`, `track_number`, and `duration_ms` for the core inheritance flow.

## Security defaults

Bridge defaults:
- `BRIDGE_HOST=127.0.0.1`
- `BRIDGE_PORT=8080`
- `BRIDGE_WS_PATH=/events`

Optional hardening:
- set `BRIDGE_SHARED_SECRET` to require `X-HSAJ-Token` on HTTP and `?token=` or the same header on WebSocket
- set `HSAJ_BRIDGE_TOKEN` in the core environment so HTTP and WS clients authenticate automatically
- configure a live Roon browse source with `BRIDGE_BLOCKED_SOURCE=roon_browse` and `BRIDGE_BLOCKED_BROWSE_SPECS='[...]'`
- or set `BRIDGE_BLOCKED_FILE=/path/to/blocked.json` / `BRIDGE_BLOCKED_JSON='[...]'` as a fallback static feed

Do not expose the bridge publicly without a shared secret.

Live blocked-source from Roon uses the official browse service exposed by the Roon extension API. Because the extension API does not expose a dedicated `blocked` endpoint directly, the bridge extracts blocked objects from configured browse paths and caches the resulting snapshot.

Example live browse configuration:

```bash
BRIDGE_BLOCKED_SOURCE=roon_browse
BRIDGE_BLOCKED_CACHE_SECONDS=60
BRIDGE_BLOCKED_BROWSE_SPECS='[
  {"name":"hidden-artists","hierarchy":"browse","path":["Hidden Artists"],"object_type":"artist"},
  {"name":"hidden-albums","hierarchy":"browse","path":["Hidden Albums"],"object_type":"album"},
  {"name":"hidden-tracks","hierarchy":"browse","path":["Hidden Tracks"],"object_type":"track","subtitle_mapping":["artist","album"]}
]'
```

Each browse spec tells the bridge which Roon browse hierarchy/path to open and how to interpret the resulting list items as `artist`, `album`, or `track` blocked objects.

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
- `POST /plan/validate`
- `POST /apply`
- `POST /restore`
- `POST /cleanup`
- `GET /candidates`
- `GET /soft-candidates`
- `GET /reviews`
- `GET /runtime-jobs`
- `POST /runtime-jobs/run`
- `POST /soft-review-preview`
- `POST /soft-review-action`
- `GET /actions`
- `GET /stats`
- `GET /exemptions`
- `POST /exemptions`
- `DELETE /exemptions/{id}`

If `security.operator_token` or `HSAJ_OPERATOR_TOKEN` is set, operator routes require
`X-HSAJ-Operator-Token`. Health, live, ready, and metrics remain available for probes.

Core health and metrics also expose the last persisted blocked-sync status, including whether the most recent bridge fetch succeeded, which blocked contract version was seen, and how many blocked objects were in the last snapshot.

Background runtime jobs can be enabled inside `hsaj serve`:

```yaml
runtime:
  enable_background_jobs: true
  blocked_sync_interval_minutes: 15
  cleanup_interval_minutes: 60
  blocked_sync_on_start: true
  cleanup_on_start: true
```

When enabled, the core runs:
- `blocked_sync`, which refreshes `/blocked` from the bridge and updates candidates
- `cleanup_retention`, which applies quarantine retention policy on a schedule

`GET /runtime-jobs` shows the persisted status of these jobs, and `POST /runtime-jobs/run`
can trigger them manually from the operator API/UI.

Stored previews are now validated before apply. The core checks that:
- the candidate is still `planned`
- the file still exists at the expected path
- the file is not exempt or Atmos-immune
- the destination does not already exist

`POST /plan/validate` returns the current validation result for a stored preview, and `POST /apply`
uses the same validation pass before executing any moves. Invalid preview items are skipped and
reported back in the response instead of being applied silently.

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
