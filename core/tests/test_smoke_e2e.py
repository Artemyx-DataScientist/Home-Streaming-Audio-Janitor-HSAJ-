from __future__ import annotations

import json
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy.orm import Session

from hsaj.cli import roon_sync_command, scan_command
from hsaj.config import DatabaseConfig, HsajConfig, PathsConfig
from hsaj.db import init_database
from hsaj.db.models import BlockCandidate, File
from hsaj.operator_service import (
    apply_preview_payload,
    cleanup_payload,
    plan_preview_payload,
    restore_payload,
    validate_preview_payload,
)
from hsaj.scanner import sync_library_graph


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_ffprobe_stub(tmp_path: Path) -> Path:
    ffprobe_path = tmp_path / "ffprobe"
    ffprobe_path.write_text("#!/usr/bin/env python3\nprint('{}')\n", encoding="utf-8")
    ffprobe_path.chmod(0o755)
    return ffprobe_path


def _write_config(tmp_path: Path, *, bridge_port: int, ffprobe_path: Path) -> Path:
    config_path = tmp_path / "hsaj.yaml"
    config_path.write_text(
        f"""
database:
  driver: sqlite
  path: {json.dumps(str(tmp_path / "hsaj.db"))}
paths:
  library_roots:
    - {json.dumps(str(tmp_path / "library"))}
  quarantine_dir: {json.dumps(str(tmp_path / "quarantine"))}
  atmos_dir: {json.dumps(str(tmp_path / "atmos"))}
  inbox_dir: {json.dumps(str(tmp_path / "inbox"))}
  ffprobe_path: {json.dumps(str(ffprobe_path))}
bridge:
  http_url: http://127.0.0.1:{bridge_port}
  ws_url: ws://127.0.0.1:{bridge_port}/events
  contract_version: v2
  required_source_mode: inline_json
runtime:
  enable_background_jobs: false
""".strip(),
        encoding="utf-8",
    )
    return config_path


def _start_fake_bridge() -> tuple[ThreadingHTTPServer, int]:
    port = _free_port()
    payload = {
        "contract_version": "v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {"configured": True, "mode": "inline_json"},
        "item_count": 1,
        "object_types": ["artist"],
        "items": [
            {
                "type": "artist",
                "id": "artist-1",
                "artist": "Artist",
                "label": "Artist",
            }
        ],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/blocked":
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def test_smoke_scan_sync_plan_validate_apply_restore_cleanup(tmp_path: Path) -> None:
    server, bridge_port = _start_fake_bridge()
    ffprobe_path = _write_ffprobe_stub(tmp_path)
    config_path = _write_config(tmp_path, bridge_port=bridge_port, ffprobe_path=ffprobe_path)

    library_file = tmp_path / "library" / "Artist" / "Album" / "track.flac"
    library_file.parent.mkdir(parents=True, exist_ok=True)
    library_file.write_text("content", encoding="utf-8")

    try:
        scan_command(config=config_path, dry_run=False)
        engine, _ = init_database(DatabaseConfig(driver="sqlite", path=tmp_path / "hsaj.db"))
        with Session(engine) as session:
            file_record = session.query(File).one()
            file_record.artist = "Artist"
            file_record.album = "Album"
            file_record.title = "Title"
            file_record.track_number = 1
            sync_library_graph(session)
            session.commit()

        roon_sync_command(
            config=config_path,
            bridge_url=None,
            grace_days=0,
            cache_tracks=False,
        )

        config = HsajConfig(
            database=DatabaseConfig(driver="sqlite", path=tmp_path / "hsaj.db"),
            paths=PathsConfig(
                library_roots=[tmp_path / "library"],
                quarantine_dir=tmp_path / "quarantine",
                atmos_dir=tmp_path / "atmos",
                inbox_dir=tmp_path / "inbox",
                ffprobe_path=str(ffprobe_path),
            ),
        )
        config.bridge.http_url = f"http://127.0.0.1:{bridge_port}"
        config.bridge.required_source_mode = "inline_json"
        engine, _ = init_database(config.database)

        with Session(engine) as session:
            preview = plan_preview_payload(session, config)
            assert preview["validation"]["valid"] is True
            assert len(preview["plan"]["blocked_quarantine_due"]) == 1

            validation = validate_preview_payload(
                session,
                config,
                preview_id=str(preview["preview_id"]),
            )
            assert validation["validation"]["valid"] is True

            apply_result = apply_preview_payload(
                session,
                config,
                preview_id=str(preview["preview_id"]),
                dry_run=False,
            )
            assert len(apply_result["quarantined"]) == 1
            candidate = session.query(BlockCandidate).one()
            assert candidate.status == "quarantined"

            restore_result = restore_payload(session, "1")
            assert restore_result["restored"] is True

            cleanup_result = cleanup_payload(session, config)
            assert cleanup_result["deleted_candidates"] == []
            assert cleanup_result["expired_candidates"] == []
    finally:
        server.shutdown()
        server.server_close()
