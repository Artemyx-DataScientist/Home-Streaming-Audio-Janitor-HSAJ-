from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hsaj.config import (
    BridgeConfig,
    DatabaseConfig,
    HsajConfig,
    ObservabilityConfig,
    PathsConfig,
    SecurityConfig,
)
from hsaj.server import serve_operator_api


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(tmp_path: Path, *, token: str | None = None) -> HsajConfig:
    return HsajConfig(
        database=DatabaseConfig(driver="sqlite", path=tmp_path / "hsaj.db"),
        paths=PathsConfig(
            library_roots=[tmp_path / "library"],
            quarantine_dir=tmp_path / "quarantine",
            atmos_dir=tmp_path / "atmos",
        ),
        bridge=BridgeConfig(),
        security=SecurityConfig(
            operator_host="127.0.0.1",
            operator_port=_free_port(),
            operator_token=token,
        ),
        observability=ObservabilityConfig(),
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

        status, metrics_payload = _text_request(f"{base_url}/metrics")
        assert status == 200
        assert "hsaj_core_files_total" in metrics_payload
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
