# Running HSAJ with systemd

These units start the bridge and keep the core operator API running as a long-lived service.
Scheduled scan/sync/cleanup is optional maintenance and is handled separately.
All files live in `configs/systemd` and assume environment settings from `/etc/hsaj/hsaj.env`.

## Units
- `hsaj-bridge.service`: starts the bridge with `node src/index.js`
- `hsaj-core.service`: runs `hsaj serve` as the canonical production entrypoint
- `hsaj-maintenance.service`: optional oneshot job for `hsaj scan`, `hsaj roon sync`, and `hsaj cleanup`
- `hsaj-core.timer`: optional timer that starts `hsaj-maintenance.service`

## Environment preparation
1. Install Node.js 18+ and Python 3.11+.
2. Deploy HSAJ to a target directory, typically `/opt/hsaj`.
3. Create a clean Python environment and install the core package and dev/runtime dependencies.
4. Copy the example core config:
   ```bash
   sudo install -d /etc/hsaj
   sudo cp configs/hsaj.example.yaml /etc/hsaj/hsaj.yaml
   sudo chown root:root /etc/hsaj/hsaj.yaml
   ```
5. Copy the systemd env file:
   ```bash
   sudo cp configs/systemd/hsaj.env.example /etc/hsaj/hsaj.env
   sudo chmod 640 /etc/hsaj/hsaj.env
   ```

Important environment variables:
- `HSAJ_ROOT`
- `HSAJ_CONFIG`
- `PATH`
- `BRIDGE_HOST`
- `BRIDGE_PORT`
- `BRIDGE_WS_PATH`
- `BRIDGE_SHARED_SECRET`
- `HSAJ_BRIDGE_HTTP`
- `HSAJ_BRIDGE_WS`
- `HSAJ_BRIDGE_TOKEN`
- `HSAJ_OPERATOR_TOKEN`
- `BRIDGE_BLOCKED_SOURCE`
- `BRIDGE_BLOCKED_CACHE_SECONDS`
- `BRIDGE_BLOCKED_BROWSE_SPECS`
- `BRIDGE_BLOCKED_FILE`
- `BRIDGE_BLOCKED_JSON`

Keep `BRIDGE_HOST=127.0.0.1` unless you have a clear reason to expose it differently. If you bind the bridge to a non-loopback host, set `BRIDGE_SHARED_SECRET`.
If you expose the core operator API beyond localhost, set `HSAJ_OPERATOR_TOKEN` and pass it as `X-HSAJ-Operator-Token`.

For a live blocked-source from Roon, set `BRIDGE_BLOCKED_SOURCE=roon_browse` and provide
`BRIDGE_BLOCKED_BROWSE_SPECS` as a JSON array describing which browse paths should be interpreted
as blocked artists/albums/tracks. Keep `BRIDGE_BLOCKED_FILE` / `BRIDGE_BLOCKED_JSON` only as
fallback modes for demos or controlled imports.

Recommended core runtime settings in `hsaj.yaml`:
```yaml
bridge:
  contract_version: v2

runtime:
  enable_background_jobs: true
  blocked_sync_interval_minutes: 15
  cleanup_interval_minutes: 60
  blocked_sync_on_start: true
  cleanup_on_start: true
```

Operational probes:
- bridge: `/live`, `/ready`, `/health`, `/metrics`
- core operator API: `/live`, `/ready`, `/health`, `/metrics`

`/ready` is dependency-aware. Expect the core to return a non-ready status if:
- required library roots are missing
- `ffprobe` cannot be resolved
- quarantine configuration is incomplete
- runtime blocked sync is stale or failing

## Install and enable
```bash
sudo install -o root -g root -m 644 configs/systemd/hsaj-bridge.service /etc/systemd/system/
sudo install -o root -g root -m 644 configs/systemd/hsaj-core.service /etc/systemd/system/
sudo install -o root -g root -m 644 configs/systemd/hsaj-maintenance.service /etc/systemd/system/
sudo install -o root -g root -m 644 configs/systemd/hsaj-core.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hsaj-bridge.service
sudo systemctl enable --now hsaj-core.service
sudo systemctl enable --now hsaj-core.timer
```

Check status:
```bash
sudo systemctl status hsaj-bridge.service
sudo systemctl status hsaj-core.service
sudo systemctl status hsaj-maintenance.service
sudo systemctl list-timers hsaj-core.timer
```
