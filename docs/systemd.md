# Running HSAJ with systemd

These units start the bridge and keep the core operator API running as a long-lived service.
The canonical production path is `hsaj serve` with built-in background jobs.
The timer-driven `scan + roon sync + apply --dry-run` path is kept only as a legacy/dev smoke workflow.
All files live in `configs/systemd` and assume environment settings from `/etc/hsaj/hsaj.env`.

## Units
- `hsaj-bridge.service`: starts the bridge with `node src/index.js`
- `hsaj-core.service`: runs `hsaj serve --config "${HSAJ_CONFIG}"` as the canonical production entrypoint
- `hsaj-maintenance.service`: optional legacy/dev oneshot smoke for `hsaj scan`, `hsaj roon sync --cache-tracks`, and `hsaj apply --dry-run`
- `hsaj-core.timer`: optional timer that starts `hsaj-maintenance.service`

## Environment preparation
1. Install Node.js 18+, Python 3.11+, and `ffprobe`.
2. Preferred path on a RoonServer host: run the installer from the repository checkout:
   ```bash
   sudo python3 tools/install_linux.py \
     --install-root /opt/hsaj \
     --config-dir /etc/hsaj \
     --generate-secrets \
     --enable-services
   ```
3. Manual path, if you do not want the installer: deploy HSAJ to a target directory, typically `/opt/hsaj`.
4. Run the clean-room bootstrap:
   ```bash
   cd /opt/hsaj
   python3 tools/bootstrap.py --recreate-venv
   ```
5. Copy the example core config:
   ```bash
   sudo install -d /etc/hsaj
   sudo cp configs/hsaj.example.yaml /etc/hsaj/hsaj.yaml
   sudo chown root:root /etc/hsaj/hsaj.yaml
   ```
6. Copy the systemd env file:
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
  required_source_mode: roon_browse_live
  max_blocked_sync_age_minutes: 30

runtime:
  enable_background_jobs: true
  blocked_sync_interval_minutes: 15
  cleanup_interval_minutes: 60
  blocked_sync_on_start: true
  cleanup_on_start: true
```

Hard-delete safety:
```yaml
policy:
  auto_delete: false
  allow_hard_delete: false
```

Physical deletion only happens when both values are `true`. Treat `allow_hard_delete` as the explicit post-backup acknowledgement gate.

Operational probes:
- bridge: `/live`, `/ready`, `/health`, `/metrics`
- core operator API: `/live`, `/ready`, `/health`, `/metrics`

`/ready` is dependency-aware. Expect the core to return a non-ready status if:
- DB bootstrap failed
- required library roots are missing
- `ffprobe` cannot be resolved
- quarantine configuration is incomplete
- `policy.auto_delete=true` without `policy.allow_hard_delete=true`
- runtime blocked sync is stale or failing
- blocked contract/source mode does not match the configured expectation

Bridge `/health` and `/ready` degrade when the blocked source is unconfigured, when the live browse provider is disconnected, or when the blocked provider reports an error. Core `/health` also surfaces destructive guardrails so operators can see why apply/cleanup is blocked.

## Install and enable
```bash
sudo install -o root -g root -m 644 configs/systemd/hsaj-bridge.service /etc/systemd/system/
sudo install -o root -g root -m 644 configs/systemd/hsaj-core.service /etc/systemd/system/
sudo install -o root -g root -m 644 configs/systemd/hsaj-maintenance.service /etc/systemd/system/
sudo install -o root -g root -m 644 configs/systemd/hsaj-core.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hsaj-bridge.service
sudo systemctl enable --now hsaj-core.service
# Optional legacy/dev smoke path only:
sudo systemctl enable --now hsaj-core.timer
```

Check status:
```bash
sudo systemctl status hsaj-bridge.service
sudo systemctl status hsaj-core.service
sudo systemctl status hsaj-maintenance.service
sudo systemctl list-timers hsaj-core.timer
```

## Backup and rollback runbook

Before enabling `policy.auto_delete=true` and `policy.allow_hard_delete=true`:

1. Take a filesystem-level backup or snapshot of the library and quarantine roots.
2. Run `tools/smoke_example.py` or `hsaj apply --dry-run` on the exact deployment config you plan to enable.
3. Verify `/health`, `/ready`, and `/metrics` on both bridge and core.
4. Confirm restore works against at least one quarantined file.
5. Only then enable hard delete and reload `hsaj-core.service`.

If rollback is needed:

1. Set `policy.auto_delete=false`.
2. Restart `hsaj-core.service`.
3. Restore files from backup or from quarantine with `hsaj restore`.
4. Inspect `actions_log` entries for `quarantine_move`, `restore_from_quarantine`, and `quarantine_delete`.
