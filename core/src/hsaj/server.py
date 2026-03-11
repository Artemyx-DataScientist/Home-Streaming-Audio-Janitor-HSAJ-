# ruff: noqa: E501, I001
from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session

from .config import HsajConfig
from .db import database_status, init_database
from .operator_service import (
    actions_payload,
    apply_preview_payload,
    candidates_payload,
    cleanup_payload,
    create_exemption_payload,
    deactivate_exemption_payload,
    exemptions_payload,
    health_payload,
    liveness_payload,
    metrics_payload,
    plan_preview_payload,
    readiness_payload,
    restore_payload,
    stats_payload,
)

logger = logging.getLogger(__name__)


OPERATOR_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>HSAJ Operator</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root { --bg: #f6f1e8; --panel: #fffdf8; --ink: #1d1b18; --accent: #8f3d2e; --line: #dccfbc; }
    body { margin: 0; font-family: Georgia, "Times New Roman", serif; background: linear-gradient(180deg, #f0e5d6 0%%, var(--bg) 100%%); color: var(--ink); }
    header { padding: 24px 28px; border-bottom: 1px solid var(--line); background: rgba(255,255,255,0.6); backdrop-filter: blur(8px); }
    h1 { margin: 0; font-size: 30px; letter-spacing: 0.04em; text-transform: uppercase; }
    main { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 18px; padding: 24px; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 18px; box-shadow: 0 10px 30px rgba(29,27,24,0.06); }
    h2 { margin-top: 0; font-size: 18px; }
    button { background: var(--accent); color: white; border: 0; border-radius: 999px; padding: 10px 16px; cursor: pointer; }
    pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.4; }
  </style>
</head>
<body>
  <header>
    <h1>HSAJ Operator</h1>
  </header>
  <main>
    <section>
      <h2>Stats</h2>
      <button onclick="refreshAll()">Refresh</button>
      <pre id="stats"></pre>
    </section>
    <section>
      <h2>Plan Preview</h2>
      <button onclick="loadPlan()">Build Preview</button>
      <button onclick="applyPreview()">Apply Preview</button>
      <pre id="plan"></pre>
    </section>
    <section>
      <h2>Candidates</h2>
      <pre id="candidates"></pre>
    </section>
    <section>
      <h2>Actions</h2>
      <pre id="actions"></pre>
    </section>
  </main>
  <script>
    let previewId = null;
    async function fetchJson(url, options) {
      const response = await fetch(url, options);
      const payload = await response.json();
      if (!response.ok) throw new Error(JSON.stringify(payload));
      return payload;
    }
    async function refreshAll() {
      document.getElementById("stats").textContent = JSON.stringify(await fetchJson("/stats"), null, 2);
      document.getElementById("candidates").textContent = JSON.stringify(await fetchJson("/candidates"), null, 2);
      document.getElementById("actions").textContent = JSON.stringify(await fetchJson("/actions"), null, 2);
    }
    async function loadPlan() {
      const payload = await fetchJson("/plan");
      previewId = payload.preview_id;
      document.getElementById("plan").textContent = JSON.stringify(payload, null, 2);
    }
    async function applyPreview() {
      const payload = await fetchJson("/apply", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({preview_id: previewId}),
      });
      document.getElementById("plan").textContent = JSON.stringify(payload, null, 2);
      await refreshAll();
    }
    refreshAll();
  </script>
</body>
</html>
"""


def serve_operator_api(config: HsajConfig) -> ThreadingHTTPServer:
    engine, _ = init_database(config.database)
    operator_token = (config.security.operator_token or "").strip() or None
    require_auth = operator_token is not None
    if require_auth and config.security.operator_host in {"127.0.0.1", "localhost", "::1"}:
        logger.info("Operator API auth enabled for loopback host")

    def _extract_token(handler: BaseHTTPRequestHandler) -> str | None:
        header = handler.headers.get("X-HSAJ-Operator-Token")
        if header:
            cleaned = header.strip()
            return cleaned or None
        token_values = parse_qs(urlparse(handler.path).query).get("token", [])
        if not token_values:
            return None
        cleaned = token_values[0].strip()
        return cleaned or None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                if not self._authorize():
                    return
                self._send_html(OPERATOR_HTML)
                return
            if parsed.path == "/live":
                self._with_session(lambda session: liveness_payload(session, config))
                return
            if parsed.path == "/health":
                self._with_session(
                    lambda session: health_payload(
                        session, config, schema_version=database_status(config.database)
                    )
                )
                return
            if parsed.path == "/ready":
                self._with_session(lambda session: readiness_payload(session, config))
                return
            if parsed.path == "/metrics":
                self._with_session_text(lambda session: metrics_payload(session, config))
                return
            if not self._authorize():
                return
            if parsed.path == "/plan":
                self._with_session(lambda session: plan_preview_payload(session, config))
                return
            if parsed.path == "/candidates":
                self._with_session(candidates_payload)
                return
            if parsed.path == "/actions":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["100"])[0])
                self._with_session(lambda session: actions_payload(session, limit=limit))
                return
            if parsed.path == "/stats":
                self._with_session(lambda session: stats_payload(session, config))
                return
            if parsed.path == "/exemptions":
                self._with_session(exemptions_payload)
                return
            self._send_json({"message": "Not Found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authorize():
                return
            body = self._read_json_body()
            if parsed.path == "/apply":
                self._with_session(
                    lambda session: apply_preview_payload(
                        session,
                        config,
                        preview_id=body.get("preview_id"),
                        dry_run=bool(body.get("dry_run", False)),
                    )
                )
                return
            if parsed.path == "/restore":
                self._with_session(lambda session: restore_payload(session, str(body["target"])))
                return
            if parsed.path == "/cleanup":
                self._with_session(lambda session: cleanup_payload(session, config))
                return
            if parsed.path == "/exemptions":
                self._with_session(lambda session: create_exemption_payload(session, body))
                return
            self._send_json({"message": "Not Found"}, status=HTTPStatus.NOT_FOUND)

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authorize():
                return
            if parsed.path.startswith("/exemptions/"):
                exemption_id = int(parsed.path.rsplit("/", 1)[-1])
                self._with_session(
                    lambda session: deactivate_exemption_payload(session, exemption_id)
                )
                return
            self._send_json({"message": "Not Found"}, status=HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            del format, args

        def _authorize(self) -> bool:
            if not require_auth:
                return True
            if _extract_token(self) == operator_token:
                return True
            self._send_json(
                {"message": "Unauthorized"},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return False

        def _with_session(self, builder: Callable[[Session], Any]) -> None:
            try:
                with Session(engine) as session:
                    payload = builder(session)
            except KeyError as exc:
                self._send_json(
                    {"message": "Not Found", "detail": str(exc)},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json(
                    {"message": "Internal Server Error", "detail": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send_json(payload)

        def _with_session_text(self, builder: Callable[[Session], str]) -> None:
            try:
                with Session(engine) as session:
                    payload = builder(session)
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json(
                    {"message": "Internal Server Error", "detail": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send_text(payload)

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw or "{}")

        def _send_json(self, payload: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self, html: str) -> None:
            raw = html.encode("utf-8")
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _send_text(self, payload: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            raw = payload.encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(
        (config.security.operator_host, config.security.operator_port), Handler
    )
    return server
