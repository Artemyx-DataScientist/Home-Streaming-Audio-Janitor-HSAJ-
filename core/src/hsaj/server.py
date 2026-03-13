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
from .guardrails import SafetyError
from .operator_service import (
    actions_payload,
    apply_preview_payload,
    candidates_payload,
    cleanup_payload,
    create_soft_review_action_payload,
    create_soft_review_preview_payload,
    create_exemption_payload,
    deactivate_exemption_payload,
    exemptions_payload,
    health_payload,
    liveness_payload,
    metrics_payload,
    plan_preview_payload,
    readiness_payload,
    reviews_payload,
    restore_payload,
    runtime_jobs_payload,
    soft_candidates_payload,
    stats_payload,
    validate_preview_payload,
)
from .runtime_jobs import BackgroundScheduler

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
    main { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; padding: 24px; }
    section { background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 18px; box-shadow: 0 10px 30px rgba(29,27,24,0.06); }
    h2 { margin-top: 0; font-size: 18px; }
    button { background: var(--accent); color: white; border: 0; border-radius: 999px; padding: 10px 16px; cursor: pointer; }
    button.secondary { background: #73614f; }
    button.muted { background: #b8a793; color: #201b17; }
    pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.4; }
    .soft-item { border-top: 1px solid var(--line); padding: 12px 0; }
    .soft-item:first-child { border-top: 0; padding-top: 0; }
    .soft-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .soft-meta { font-size: 12px; opacity: 0.8; }
  </style>
</head>
<body>
  <header>
    <h1>HSAJ Operator</h1>
  </header>
  <main>
    <section>
      <h2>Health</h2>
      <pre id="health"></pre>
      <pre id="ready"></pre>
    </section>
    <section>
      <h2>Stats</h2>
      <button onclick="refreshAll()">Refresh</button>
      <pre id="stats"></pre>
    </section>
    <section>
      <h2>Plan Preview</h2>
      <button onclick="loadPlan()">Build Preview</button>
      <button class="secondary" onclick="validatePreview()">Validate Preview</button>
      <button onclick="applyPreview()">Apply Preview</button>
      <pre id="plan"></pre>
    </section>
    <section>
      <h2>Candidates</h2>
      <pre id="candidates"></pre>
    </section>
    <section>
      <h2>Soft Review</h2>
      <div id="soft-candidates"></div>
    </section>
    <section>
      <h2>Actions</h2>
      <pre id="actions"></pre>
    </section>
    <section>
      <h2>Review History</h2>
      <pre id="reviews"></pre>
    </section>
    <section>
      <h2>Runtime Jobs</h2>
      <div class="soft-actions">
        <button onclick="runRuntimeJob('blocked_sync')">Run Blocked Sync</button>
        <button class="secondary" onclick="runRuntimeJob('cleanup_retention')">Run Cleanup</button>
      </div>
      <pre id="runtime-jobs"></pre>
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
      document.getElementById("health").textContent = JSON.stringify(await fetchJson("/health"), null, 2);
      document.getElementById("ready").textContent = JSON.stringify(await fetchJson("/ready"), null, 2);
      document.getElementById("stats").textContent = JSON.stringify(await fetchJson("/stats"), null, 2);
      document.getElementById("candidates").textContent = JSON.stringify(await fetchJson("/candidates"), null, 2);
      document.getElementById("actions").textContent = JSON.stringify(await fetchJson("/actions"), null, 2);
      document.getElementById("reviews").textContent = JSON.stringify(await fetchJson("/reviews"), null, 2);
      document.getElementById("runtime-jobs").textContent = JSON.stringify(await fetchJson("/runtime-jobs"), null, 2);
      await refreshSoftCandidates();
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
    async function validatePreview() {
      const payload = await fetchJson("/plan/validate", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({preview_id: previewId}),
      });
      document.getElementById("plan").textContent = JSON.stringify(payload, null, 2);
    }
    async function refreshSoftCandidates() {
      const payload = await fetchJson("/soft-candidates");
      const root = document.getElementById("soft-candidates");
      root.innerHTML = "";
      if (!payload.length) {
        root.textContent = "No soft candidates.";
        return;
      }
      payload.forEach((item) => {
        const box = document.createElement("div");
        box.className = "soft-item";
        const title = document.createElement("strong");
        title.textContent = item.reason + " #" + item.file_id;
        box.appendChild(title);
        const meta = document.createElement("div");
        meta.className = "soft-meta";
        meta.textContent = item.source + (item.review_status ? " | review: " + item.review_status : "");
        box.appendChild(meta);
        const evidence = document.createElement("pre");
        evidence.textContent = JSON.stringify(item.evidence, null, 2);
        box.appendChild(evidence);
        const actions = document.createElement("div");
        actions.className = "soft-actions";
        actions.appendChild(actionButton("Preview Quarantine", async () => {
          const preview = await fetchJson("/soft-review-preview", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({selections: [{file_id: item.file_id, reason: item.reason}]}),
          });
          previewId = preview.preview_id;
          document.getElementById("plan").textContent = JSON.stringify(preview, null, 2);
        }));
        actions.appendChild(actionButton("Dismiss", async () => {
          await fetchJson("/soft-review-action", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({file_id: item.file_id, reason: item.reason, action: "dismiss"}),
          });
          await refreshAll();
        }, "secondary"));
        actions.appendChild(actionButton("Exempt", async () => {
          await fetchJson("/soft-review-action", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({file_id: item.file_id, reason: item.reason, action: "exempt"}),
          });
          await refreshAll();
        }, "muted"));
        box.appendChild(actions);
        root.appendChild(box);
      });
    }
    function actionButton(label, onclick, className) {
      const button = document.createElement("button");
      button.textContent = label;
      button.onclick = onclick;
      if (className) button.className = className;
      return button;
    }
    async function runRuntimeJob(jobName) {
      const payload = await fetchJson("/runtime-jobs/run", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({job_name: jobName}),
      });
      document.getElementById("runtime-jobs").textContent = JSON.stringify(payload, null, 2);
      await refreshAll();
    }
    refreshAll();
  </script>
</body>
</html>
"""


def serve_operator_api(config: HsajConfig) -> ThreadingHTTPServer:
    engine = None
    schema_version = None
    boot_error = None
    try:
        engine, schema_version = init_database(config.database)
    except Exception as exc:  # pragma: no cover - exercised via HTTP boundary tests
        boot_error = f"Database init failed: {exc}"
    scheduler = (
        BackgroundScheduler(engine=engine, config=config)
        if config.runtime.enable_background_jobs and engine is not None
        else None
    )
    if scheduler is not None:
        scheduler.start()
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
                if boot_error is not None and engine is None:
                    payload = health_payload(
                        None,
                        config,
                        schema_version=schema_version,
                        boot_error=boot_error,
                    )
                    self._send_json(
                        payload,
                        status=(
                            HTTPStatus.OK
                            if payload.get("status") == "ok"
                            else HTTPStatus.SERVICE_UNAVAILABLE
                        ),
                    )
                    return
                self._with_session(
                    lambda session: health_payload(
                        session,
                        config,
                        schema_version=(
                            schema_version
                            if boot_error is not None
                            else (schema_version or database_status(config.database))
                        ),
                        boot_error=boot_error,
                    ),
                    status_builder=lambda payload: (
                        HTTPStatus.OK
                        if payload.get("status") == "ok"
                        else HTTPStatus.SERVICE_UNAVAILABLE
                    ),
                )
                return
            if parsed.path == "/ready":
                if boot_error is not None and engine is None:
                    payload = readiness_payload(None, config, boot_error=boot_error)
                    self._send_json(
                        payload,
                        status=(
                            HTTPStatus.OK
                            if payload.get("status") == "ready"
                            else HTTPStatus.SERVICE_UNAVAILABLE
                        ),
                    )
                    return
                self._with_session(
                    lambda session: readiness_payload(session, config, boot_error=boot_error),
                    status_builder=lambda payload: (
                        HTTPStatus.OK
                        if payload.get("status") == "ready"
                        else HTTPStatus.SERVICE_UNAVAILABLE
                    ),
                )
                return
            if parsed.path == "/metrics":
                if boot_error is not None and engine is None:
                    self._send_text(metrics_payload(None, config, boot_error=boot_error))
                    return
                self._with_session_text(
                    lambda session: metrics_payload(session, config, boot_error=boot_error)
                )
                return
            if not self._authorize():
                return
            if parsed.path == "/plan":
                self._with_session(lambda session: plan_preview_payload(session, config))
                return
            if parsed.path == "/candidates":
                self._with_session(candidates_payload)
                return
            if parsed.path == "/soft-candidates":
                self._with_session(lambda session: soft_candidates_payload(session, config))
                return
            if parsed.path == "/actions":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["100"])[0])
                self._with_session(lambda session: actions_payload(session, limit=limit))
                return
            if parsed.path == "/reviews":
                self._with_session(reviews_payload)
                return
            if parsed.path == "/runtime-jobs":
                self._with_session(lambda session: runtime_jobs_payload(session, config))
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
            if parsed.path == "/plan/validate":
                self._with_session(
                    lambda session: validate_preview_payload(
                        session,
                        config,
                        preview_id=str(body["preview_id"]),
                    )
                )
                return
            if parsed.path == "/restore":
                self._with_session(lambda session: restore_payload(session, str(body["target"])))
                return
            if parsed.path == "/cleanup":
                self._with_session(lambda session: cleanup_payload(session, config))
                return
            if parsed.path == "/soft-review-preview":
                self._with_session(
                    lambda session: create_soft_review_preview_payload(session, config, body)
                )
                return
            if parsed.path == "/soft-review-action":
                self._with_session(lambda session: create_soft_review_action_payload(session, body))
                return
            if parsed.path == "/runtime-jobs/run":
                self._run_runtime_job(str(body["job_name"]))
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

        def _with_session(
            self,
            builder: Callable[[Session], Any],
            *,
            status_builder: Callable[[Any], HTTPStatus] | None = None,
        ) -> None:
            try:
                if boot_error is not None and engine is None:
                    raise SafetyError(boot_error)
                with Session(engine) as session:
                    payload = builder(session)
            except KeyError as exc:
                self._send_json(
                    {"message": "Not Found", "detail": str(exc)},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            except SafetyError as exc:
                self._send_json(
                    {"message": "Service Unavailable", "detail": str(exc)},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json(
                    {"message": "Internal Server Error", "detail": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            status = status_builder(payload) if status_builder is not None else HTTPStatus.OK
            self._send_json(payload, status=status)

        def _with_session_text(self, builder: Callable[[Session], str]) -> None:
            try:
                if boot_error is not None and engine is None:
                    raise SafetyError(boot_error)
                with Session(engine) as session:
                    payload = builder(session)
            except SafetyError as exc:
                self._send_json(
                    {"message": "Service Unavailable", "detail": str(exc)},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json(
                    {"message": "Internal Server Error", "detail": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send_text(payload)

        def _run_runtime_job(self, job_name: str) -> None:
            if scheduler is None:
                self._send_json(
                    {"message": "Runtime background jobs are disabled"},
                    status=HTTPStatus.CONFLICT,
                )
                return
            try:
                payload = scheduler.run_job_now(job_name)
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
    original_shutdown = server.shutdown
    original_server_close = server.server_close

    def _shutdown() -> None:
        if scheduler is not None:
            scheduler.stop()
        original_shutdown()

    def _server_close() -> None:
        if scheduler is not None:
            scheduler.stop()
        original_server_close()

    server.shutdown = _shutdown  # type: ignore[assignment]
    server.server_close = _server_close  # type: ignore[assignment]
    return server
