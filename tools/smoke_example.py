from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "configs" / "hsaj.example.yaml"
CORE_DIR = REPO_ROOT / "core"
BRIDGE_DIR = REPO_ROOT / "bridge"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hsaj-smoke-") as workspace_raw:
        workspace = Path(workspace_raw)
        library_file = workspace / "library" / "Artist" / "Album" / "track.flac"
        library_file.parent.mkdir(parents=True, exist_ok=True)
        library_file.write_text("content", encoding="utf-8")

        ffprobe_path = write_ffprobe_stub(workspace)
        bridge_port = free_port()
        core_port = free_port()
        config_path = write_runtime_config(
            workspace,
            ffprobe_path=ffprobe_path,
            bridge_port=bridge_port,
            core_port=core_port,
        )

        bridge_process = start_bridge(bridge_port)
        core_process = None
        try:
            wait_for_json(
                f"http://127.0.0.1:{bridge_port}/ready",
                expected_status=200,
            )
            run_core_cli(["scan", "--config", str(config_path)])

            core_process = start_core(config_path)
            wait_for_json(
                f"http://127.0.0.1:{core_port}/ready",
                expected_status=200,
                timeout=20.0,
            )

            runtime_jobs = wait_for_json(
                f"http://127.0.0.1:{core_port}/runtime-jobs",
                expected_status=200,
            )
            blocked_job = next(item for item in runtime_jobs if item["job_name"] == "blocked_sync")
            if blocked_job["status"] != "ok":
                raise RuntimeError(f"blocked_sync did not become healthy: {blocked_job}")

            preview = request_json("GET", f"http://127.0.0.1:{core_port}/plan")
            preview_id = preview["preview_id"]
            move = preview["plan"]["blocked_quarantine_due"][0]
            file_id = move["file_id"]

            validation = request_json(
                "POST",
                f"http://127.0.0.1:{core_port}/plan/validate",
                body={"preview_id": preview_id},
            )
            if not validation["validation"]["valid"]:
                raise RuntimeError(f"preview validation failed: {validation}")

            apply_result = request_json(
                "POST",
                f"http://127.0.0.1:{core_port}/apply",
                body={"preview_id": preview_id},
            )
            if len(apply_result["quarantined"]) != 1:
                raise RuntimeError(f"expected one quarantined file, got {apply_result}")

            restore_result = request_json(
                "POST",
                f"http://127.0.0.1:{core_port}/restore",
                body={"target": str(file_id)},
            )
            if not restore_result["restored"]:
                raise RuntimeError(f"restore failed: {restore_result}")

            cleanup_result = request_json(
                "POST",
                f"http://127.0.0.1:{core_port}/cleanup",
                body={},
            )
            if cleanup_result["deleted_candidates"] or cleanup_result["expired_candidates"]:
                raise RuntimeError(f"unexpected cleanup result: {cleanup_result}")
        finally:
            terminate_process(core_process)
            terminate_process(bridge_process)
    print("Smoke scenario completed successfully.")
    return 0


def write_runtime_config(
    workspace: Path,
    *,
    ffprobe_path: Path,
    bridge_port: int,
    core_port: int,
) -> Path:
    config = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    config["database"]["path"] = str(workspace / "data" / "hsaj.db")
    config["paths"]["library_roots"] = [str(workspace / "library")]
    config["paths"]["quarantine_dir"] = str(workspace / "quarantine")
    config["paths"]["atmos_dir"] = str(workspace / "atmos")
    config["paths"]["inbox_dir"] = str(workspace / "inbox")
    config["paths"]["ffprobe_path"] = str(ffprobe_path)
    config["policy"]["auto_delete"] = False
    config["policy"]["allow_hard_delete"] = False
    config["bridge"]["http_url"] = f"http://127.0.0.1:{bridge_port}"
    config["bridge"]["ws_url"] = f"ws://127.0.0.1:{bridge_port}/events"
    config["bridge"]["required_source_mode"] = "inline_json"
    config["bridge"]["max_blocked_sync_age_minutes"] = 30
    config["security"]["operator_port"] = core_port
    config["runtime"]["enable_background_jobs"] = True
    config["runtime"]["blocked_sync_interval_minutes"] = 15
    config["runtime"]["cleanup_interval_minutes"] = 60
    config["runtime"]["blocked_sync_on_start"] = True
    config["runtime"]["cleanup_on_start"] = True

    config_path = workspace / "hsaj.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path


def write_ffprobe_stub(workspace: Path) -> Path:
    bin_dir = workspace / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        ffprobe_path = bin_dir / "ffprobe.cmd"
        ffprobe_path.write_text("@echo {}\r\n", encoding="utf-8")
    else:
        ffprobe_path = bin_dir / "ffprobe"
        ffprobe_path.write_text("#!/usr/bin/env sh\necho '{}'\n", encoding="utf-8")
        ffprobe_path.chmod(0o755)
    return ffprobe_path


def start_bridge(port: int) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(
        {
            "BRIDGE_HOST": "127.0.0.1",
            "BRIDGE_PORT": str(port),
            "BRIDGE_BLOCKED_JSON": json.dumps(
                [{"type": "artist", "id": "artist-1", "artist": "Artist", "label": "Artist"}]
            ),
        }
    )
    return subprocess.Popen(
        ["node", "src/index.js"],
        cwd=BRIDGE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def start_core(config_path: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "hsaj.cli", "serve", "--config", str(config_path)],
        cwd=CORE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def run_core_cli(args: list[str]) -> None:
    command = [sys.executable, "-m", "hsaj.cli", *args]
    completed = subprocess.run(
        command,
        cwd=CORE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )


def request_json(method: str, url: str, *, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:  # noqa: S310 - local smoke against local services
        return json.loads(response.read().decode("utf-8"))


def wait_for_json(url: str, *, expected_status: int, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=2) as response:  # noqa: S310 - local smoke
                if response.status == expected_status:
                    return json.loads(response.read().decode("utf-8"))
                last_error = RuntimeError(f"unexpected status {response.status} for {url}")
        except HTTPError as exc:
            if exc.code == expected_status:
                return json.loads(exc.read().decode("utf-8"))
            last_error = exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def terminate_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    raise SystemExit(main())
