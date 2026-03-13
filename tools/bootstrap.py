from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import venv
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / "core"
BRIDGE_DIR = REPO_ROOT / "bridge"
DEFAULT_VENV = CORE_DIR / ".venv"


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap HSAJ dependencies and smoke entrypoint")
    parser.add_argument("--venv", type=Path, default=DEFAULT_VENV, help="Python virtualenv path")
    parser.add_argument(
        "--recreate-venv",
        action="store_true",
        help="Delete and recreate the virtual environment before installing",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Only run environment checks; do not install dependencies",
    )
    parser.add_argument(
        "--run-smoke",
        action="store_true",
        help="Run the example smoke scenario after dependency installation",
    )
    args = parser.parse_args()

    ensure_python_version()
    ensure_cli_version("node", 18)
    ensure_cli_available("npm", ["--version"])
    ensure_cli_available("ffprobe", ["-version"])

    venv_path = args.venv.resolve()
    if not args.skip_install:
        recreate_virtualenv(venv_path, recreate=args.recreate_venv)
        python_bin = venv_python(venv_path)
        run([str(python_bin), "-m", "pip", "install", "--upgrade", "pip"], cwd=REPO_ROOT)
        run(
            [str(python_bin), "-m", "pip", "install", "-r", "requirements-dev.txt"],
            cwd=CORE_DIR,
        )
        run(["npm", "ci"], cwd=BRIDGE_DIR)
    else:
        python_bin = venv_python(venv_path)

    if args.run_smoke:
        run([str(python_bin), str(REPO_ROOT / "tools" / "smoke_example.py")], cwd=REPO_ROOT)

    print("Bootstrap completed successfully.")
    print(f"Virtualenv: {venv_path}")
    print(f"Smoke entrypoint: {python_bin} {REPO_ROOT / 'tools' / 'smoke_example.py'}")
    return 0


def ensure_python_version() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit(
            f"Python 3.11+ is required, got {sys.version_info.major}.{sys.version_info.minor}"
        )


def ensure_cli_available(command: str, version_args: list[str]) -> None:
    try:
        completed = subprocess.run(
            [command, *version_args],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Required dependency '{command}' is not available on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"Could not execute '{command} {' '.join(version_args)}': {exc.stderr or exc.stdout}"
        ) from exc
    if completed.stdout:
        print(completed.stdout.splitlines()[0])
    elif completed.stderr:
        print(completed.stderr.splitlines()[0])


def ensure_cli_version(command: str, minimum_major: int) -> None:
    try:
        completed = subprocess.run(
            [command, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Required dependency '{command}' is not available on PATH") from exc
    output = (completed.stdout or completed.stderr).strip()
    if not output:
        raise SystemExit(f"Could not read version output from '{command} --version'")
    version_token = output.lstrip("v").split(".", 1)[0]
    try:
        major = int(version_token)
    except ValueError as exc:
        raise SystemExit(f"Could not parse {command} version from '{output}'") from exc
    if major < minimum_major:
        raise SystemExit(f"{command} {minimum_major}+ is required, got {output}")
    print(output)


def recreate_virtualenv(path: Path, *, recreate: bool) -> None:
    if recreate and path.exists():
        shutil.rmtree(path)
    if path.exists():
        return
    print(f"Creating virtualenv at {path}")
    builder = venv.EnvBuilder(with_pip=True, clear=False)
    builder.create(path)


def venv_python(path: Path) -> Path:
    python_path = (
        path / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else path / "bin" / "python"
    )
    if not python_path.exists():
        raise SystemExit(f"Virtualenv python not found at {python_path}")
    return python_path


def run(command: list[str], *, cwd: Path) -> None:
    print(f"[run] {' '.join(command)}")
    subprocess.run(command, cwd=cwd, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
