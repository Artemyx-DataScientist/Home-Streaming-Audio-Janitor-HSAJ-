from __future__ import annotations

import json
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from hsaj.blocking import BlockedObject, BlockedSnapshot, record_blocked_sync_success
from hsaj.config import (
    BridgeConfig,
    DatabaseConfig,
    HsajConfig,
    ObservabilityConfig,
    PathsConfig,
    RuntimeConfig,
    SecurityConfig,
)
from hsaj.db import init_database
from hsaj.db.models import File
from hsaj.scanner import sync_library_graph
from hsaj.server import serve_operator_api


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(
    tmp_path: Path,
    *,
    token: str | None = None,
    runtime_enabled: bool = False,
) -> HsajConfig:
    library_root = tmp_path / "library"
    quarantine_dir = tmp_path / "quarantine"
    atmos_dir = tmp_path / "atmos"
    ffprobe_path = tmp_path / "bin" / "ffprobe"
    library_root.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    atmos_dir.mkdir(parents=True, exist_ok=True)
    ffprobe_path.parent.mkdir(parents=True, exist_ok=True)
    ffprobe_path.write_text("stub")
    return HsajConfig(
        database=DatabaseConfig(driver="sqlite", path=tmp_path / "hsaj.db"),
        paths=PathsConfig(
            library_roots=[library_root],
            quarantine_dir=quarantine_dir,
            atmos_dir=atmos_dir,
            ffprobe_path=str(ffprobe_path),
        ),
        bridge=BridgeConfig(),
        security=SecurityConfig(
            operator_host="127.0.0.1",
            operator_port=_free_port(),
            operator_token=token,
        ),
        observability=ObservabilityConfig(),
        runtime=RuntimeConfig(
            enable_background_jobs=runtime_enabled,
            blocked_sync_on_start=False,
            cleanup_on_start=False,
        ),
    )


def _json_request(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token is not None:
        headers["X-HSAJ-Operator-Token"] = token
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:  # noqa: S310
        return response.status, json.loads(response.read().decode("utf-8"))


def _text_request(url: str) -> tuple[int, str]:
    with urlopen(url, timeout=5) as response:  # noqa: S310
        return response.status, response.read().decode("utf-8")


def test_server_exposes_live_ready_and_metrics(tmp_path: Path) -> None:
    config = _config(tmp_path)
    server = serve_operator_api(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{config.security.operator_host}:{config.security.operator_port}"

    try:
        status, live_payload = _json_request("GET", f"{base_url}/live")
        assert status == 200
        assert live_payload["status"] == "live"

        status, ready_payload = _json_request("GET", f"{base_url}/ready")
        assert status == 200
        assert ready_payload["status"] == "ready"
        assert all(check["ok"] is True for check in ready_payload["checks"])

        status, metrics_payload = _text_request(f"{base_url}/metrics")
        assert status == 200
        assert "hsaj_core_files_total" in metrics_payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_ready_returns_503_when_dependencies_are_missing(tmp_path: Path) -> None:
    config = _config(tmp_path, runtime_enabled=True)
    config.paths.library_roots = [tmp_path / "missing-library"]
    config.paths.ffprobe_path = str(tmp_path / "missing-bin" / "ffprobe")
    server = serve_operator_api(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{config.security.operator_host}:{config.security.operator_port}"

    try:
        try:
            _json_request("GET", f"{base_url}/ready")
            raise AssertionError("Expected readiness failure")
        except HTTPError as exc:
            assert exc.code == 503
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["status"] == "not_ready"
            assert any(check["ok"] is False for check in payload["checks"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_enforces_operator_token_for_operator_routes(tmp_path: Path) -> None:
    config = _config(tmp_path, token="secret-token")
    server = serve_operator_api(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{config.security.operator_host}:{config.security.operator_port}"

    try:
        try:
            _json_request("GET", f"{base_url}/plan")
            raise AssertionError("Expected unauthorized request to fail")
        except HTTPError as exc:
            assert exc.code == 401

        status, payload = _json_request(
            "GET",
            f"{base_url}/plan",
            token="secret-token",
        )
        assert status == 200
        assert "preview_id" in payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_health_exposes_blocked_sync_status(tmp_path: Path) -> None:
    config = _config(tmp_path)

    engine, _ = init_database(config.database)
    with Session(engine) as session:
        record_blocked_sync_success(
            session,
            snapshot=BlockedSnapshot(
                items=[BlockedObject(object_type="artist", object_id="artist-1", artist="Artist")],
                contract_version="v2",
                generated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                source_mode="inline_json",
                item_count=1,
            ),
            attempted_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        )
        session.commit()

    server = serve_operator_api(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{config.security.operator_host}:{config.security.operator_port}"

    try:
        status, health_payload = _json_request("GET", f"{base_url}/health")
        assert status == 200
        assert health_payload["blocked_sync"]["status"] == "ok"
        assert health_payload["blocked_sync"]["contract_version"] == "v2"

        status, metrics_payload = _text_request(f"{base_url}/metrics")
        assert status == 200
        assert "hsaj_core_blocked_sync_ok 1" in metrics_payload
        assert "hsaj_core_blocked_sync_item_count 1" in metrics_payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_supports_soft_review_flow(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library_file = config.paths.library_roots[0] / "Artist" / "Album" / "track.mp3"
    library_file.parent.mkdir(parents=True, exist_ok=True)
    library_file.write_text("content")

    engine, _ = init_database(config.database)
    with Session(engine) as session:
        file_record = File(
            path=str(library_file),
            size_bytes=1,
            format="mp3",
            mtime=datetime(2023, 1, 1, tzinfo=timezone.utc),
            artist="Artist",
            album="Album",
            title="Title",
            track_number=1,
            year=2024,
            duration_seconds=300,
        )
        session.add(file_record)
        session.commit()
        sync_library_graph(session)
        session.commit()
        file_id = file_record.id

    server = serve_operator_api(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{config.security.operator_host}:{config.security.operator_port}"

    try:
        status, soft_payload = _json_request("GET", f"{base_url}/soft-candidates")
        assert status == 200
        assert len(soft_payload) == 1
        assert soft_payload[0]["reason"] == "never_played_old"

        status, preview_payload = _json_request(
            "POST",
            f"{base_url}/soft-review-preview",
            body={"selections": [{"file_id": file_id, "reason": "never_played_old"}]},
        )
        assert status == 200
        assert preview_payload["plan"]["blocked_quarantine_due"][0]["reason"] == (
            "soft_review:never_played_old"
        )

        status, apply_payload = _json_request(
            "POST",
            f"{base_url}/apply",
            body={"preview_id": preview_payload["preview_id"]},
        )
        assert status == 200
        assert len(apply_payload["quarantined"]) == 1

        status, reviews_payload = _json_request("GET", f"{base_url}/reviews")
        assert status == 200
        assert reviews_payload[0]["action"] == "quarantined"

        status, actions_payload = _json_request("GET", f"{base_url}/actions")
        assert status == 200
        assert any(item["action"] == "quarantine_move" for item in actions_payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_can_dismiss_soft_candidate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library_file = config.paths.library_roots[0] / "Artist" / "Album" / "track.mp3"
    library_file.parent.mkdir(parents=True, exist_ok=True)
    library_file.write_text("content")

    engine, _ = init_database(config.database)
    with Session(engine) as session:
        file_record = File(
            path=str(library_file),
            size_bytes=1,
            format="mp3",
            mtime=datetime(2023, 1, 1, tzinfo=timezone.utc),
            artist="Artist",
            album="Album",
            title="Title",
            track_number=1,
            year=2024,
            duration_seconds=300,
        )
        session.add(file_record)
        session.commit()
        sync_library_graph(session)
        session.commit()
        file_id = file_record.id

    server = serve_operator_api(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{config.security.operator_host}:{config.security.operator_port}"

    try:
        status, _ = _json_request(
            "POST",
            f"{base_url}/soft-review-action",
            body={"file_id": file_id, "reason": "never_played_old", "action": "dismiss"},
        )
        assert status == 200

        status, soft_payload = _json_request("GET", f"{base_url}/soft-candidates")
        assert status == 200
        assert soft_payload == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_can_run_runtime_job_manually(tmp_path: Path) -> None:
    config = _config(tmp_path, runtime_enabled=True)
    server = serve_operator_api(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{config.security.operator_host}:{config.security.operator_port}"

    try:
        status, jobs_payload = _json_request("GET", f"{base_url}/runtime-jobs")
        assert status == 200
        assert {item["job_name"] for item in jobs_payload} == {
            "blocked_sync",
            "cleanup_retention",
        }

        status, payload = _json_request(
            "POST",
            f"{base_url}/runtime-jobs/run",
            body={"job_name": "cleanup_retention"},
        )
        assert status == 200
        assert payload["job_name"] == "cleanup_retention"
        assert payload["status"] == "ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_can_validate_stale_preview(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library_file = config.paths.library_roots[0] / "Artist" / "Album" / "track.flac"
    library_file.parent.mkdir(parents=True, exist_ok=True)
    library_file.write_text("content")

    from hsaj.db.models import BlockCandidate, RoonItemCache
    from hsaj.exemptions import add_exemption

    engine, _ = init_database(config.database)
    with Session(engine) as session:
        file_record = File(
            path=str(library_file),
            size_bytes=1,
            format="flac",
            mtime=datetime(2024, 1, 1, tzinfo=timezone.utc),
            artist="Artist",
            album="Album",
            title="Title",
            track_number=1,
            year=2024,
            duration_seconds=300,
        )
        session.add(file_record)
        session.add(
            BlockCandidate(
                object_type="track",
                object_id="track-1",
                label="Track",
                metadata_json='{"artist":"Artist","album":"Album","title":"Title","track_number":1,"duration_ms":300000}',
                reason="blocked_by_track",
                status="planned",
                source="bridge.blocked.v1",
                rule_id="blocked_by_track",
                first_seen_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                last_seen_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                planned_action_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
                last_transition_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
        )
        session.add(
            RoonItemCache(
                roon_track_id="track-1",
                artist="Artist",
                album="Album",
                title="Title",
                track_number=1,
                duration_ms=300000,
            )
        )
        session.commit()

    server = serve_operator_api(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{config.security.operator_host}:{config.security.operator_port}"

    try:
        status, preview_payload = _json_request("GET", f"{base_url}/plan")
        assert status == 200
        assert preview_payload["validation"]["valid"] is True

        with Session(engine) as session:
            add_exemption(
                session,
                scope_type="path",
                path=str(library_file),
                reason="hold",
            )
            session.commit()

        status, validation_payload = _json_request(
            "POST",
            f"{base_url}/plan/validate",
            body={"preview_id": preview_payload["preview_id"]},
        )
        assert status == 200
        assert validation_payload["validation"]["valid"] is False
        assert validation_payload["validation"]["issues"][0]["code"] == "exempt"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
