from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INSTALL_ROOT = Path("/opt/hsaj")
DEFAULT_CONFIG_DIR = Path("/etc/hsaj")
SYSTEMD_DIR = Path("/etc/systemd/system")
COPY_ITEMS = [
    "bridge",
    "core",
    "configs",
    "docs",
    "tools",
    "README.md",
    "ARCHITECTURE.md",
    "DESIGN.md",
    "SPEC.md",
    "adr",
]
IGNORE_NAMES = shutil.ignore_patterns(
    ".git",
    ".github",
    ".idea",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    ".venv",
    "tmp-smoke-debug*",
    "tmp-smoke-debug2*",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install HSAJ on a Linux/systemd host that also runs RoonServer."
    )
    parser.add_argument("--install-root", type=Path, default=DEFAULT_INSTALL_ROOT)
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument(
        "--overwrite-config",
        action="store_true",
        help="Replace existing hsaj.yaml and hsaj.env with the example templates.",
    )
    parser.add_argument(
        "--recreate-venv",
        action="store_true",
        help="Recreate core/.venv during bootstrap.",
    )
    parser.add_argument(
        "--enable-services",
        action="store_true",
        help="Run systemctl enable --now for bridge and core after installation.",
    )
    parser.add_argument(
        "--enable-legacy-timer",
        action="store_true",
        help="Also enable the legacy hsaj-core.timer smoke workflow.",
    )
    parser.add_argument(
        "--run-smoke",
        action="store_true",
        help="Run tools/smoke_example.py after bootstrap.",
    )
    parser.add_argument(
        "--generate-secrets",
        action="store_true",
        help="Generate BRIDGE_SHARED_SECRET and HSAJ_OPERATOR_TOKEN in hsaj.env if blank.",
    )
    args = parser.parse_args()

    ensure_linux_root()

    install_root = args.install_root.resolve()
    config_dir = args.config_dir.resolve()
    env_path = config_dir / "hsaj.env"
    config_path = config_dir / "hsaj.yaml"

    copy_repo(install_root)
    write_config_templates(
        install_root=install_root,
        config_dir=config_dir,
        config_path=config_path,
        env_path=env_path,
        overwrite=args.overwrite_config,
        generate_secrets=args.generate_secrets,
    )
    run_bootstrap(install_root, recreate_venv=args.recreate_venv, run_smoke=args.run_smoke)
    install_systemd_units(install_root)
    daemon_reload()
    if args.enable_services:
        enable_services(enable_legacy_timer=args.enable_legacy_timer)

    print("HSAJ installation completed.")
    print(f"Install root: {install_root}")
    print(f"Config: {config_path}")
    print(f"Env: {env_path}")
    print("Next step: edit /etc/hsaj/hsaj.yaml and /etc/hsaj/hsaj.env for your RoonServer host.")
    print("Then verify:")
    print("  systemctl status hsaj-bridge.service")
    print("  systemctl status hsaj-core.service")
    print("  curl -fsS http://127.0.0.1:8080/ready | jq .")
    print("  curl -fsS http://127.0.0.1:8090/ready | jq .")
    return 0


def ensure_linux_root() -> None:
    if os.name != "posix":
        raise SystemExit("tools/install_linux.py must be run on a Linux host.")
    if not Path("/run/systemd/system").exists():
        raise SystemExit("systemd was not detected at /run/systemd/system.")
    if os.geteuid() != 0:
        raise SystemExit("Run this installer as root or via sudo.")


def copy_repo(install_root: Path) -> None:
    install_root.mkdir(parents=True, exist_ok=True)
    for item_name in COPY_ITEMS:
        source = REPO_ROOT / item_name
        target = install_root / item_name
        if not source.exists():
            continue
        if source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, ignore=IGNORE_NAMES)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def write_config_templates(
    *,
    install_root: Path,
    config_dir: Path,
    config_path: Path,
    env_path: Path,
    overwrite: bool,
    generate_secrets: bool,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)

    example_config = install_root / "configs" / "hsaj.example.yaml"
    example_env = install_root / "configs" / "systemd" / "hsaj.env.example"
    if overwrite or not config_path.exists():
        shutil.copy2(example_config, config_path)
    if overwrite or not env_path.exists():
        shutil.copy2(example_env, env_path)

    env_lines = env_path.read_text(encoding="utf-8").splitlines()
    env_lines = set_env_var(env_lines, "HSAJ_ROOT", str(install_root))
    env_lines = set_env_var(env_lines, "HSAJ_CONFIG", str(config_path))
    env_lines = set_env_var(
        env_lines,
        "PATH",
        f"{install_root}/core/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin",
    )
    if generate_secrets:
        env_lines = set_env_var_if_blank(env_lines, "BRIDGE_SHARED_SECRET", secrets.token_urlsafe(24))
        env_lines = set_env_var_if_blank(env_lines, "HSAJ_OPERATOR_TOKEN", secrets.token_urlsafe(24))
        env_lines = set_env_var_if_blank(
            env_lines,
            "HSAJ_BRIDGE_TOKEN",
            get_env_value(env_lines, "BRIDGE_SHARED_SECRET") or secrets.token_urlsafe(24),
        )
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")


def run_bootstrap(install_root: Path, *, recreate_venv: bool, run_smoke: bool) -> None:
    command = [sys.executable, str(install_root / "tools" / "bootstrap.py")]
    if recreate_venv:
        command.append("--recreate-venv")
    if run_smoke:
        command.append("--run-smoke")
    run(command, cwd=install_root)


def install_systemd_units(install_root: Path) -> None:
    units_dir = install_root / "configs" / "systemd"
    for unit_name in (
        "hsaj-bridge.service",
        "hsaj-core.service",
        "hsaj-maintenance.service",
        "hsaj-core.timer",
    ):
        shutil.copy2(units_dir / unit_name, SYSTEMD_DIR / unit_name)


def daemon_reload() -> None:
    run(["systemctl", "daemon-reload"], cwd=REPO_ROOT)


def enable_services(*, enable_legacy_timer: bool) -> None:
    run(["systemctl", "enable", "--now", "hsaj-bridge.service"], cwd=REPO_ROOT)
    run(["systemctl", "enable", "--now", "hsaj-core.service"], cwd=REPO_ROOT)
    if enable_legacy_timer:
        run(["systemctl", "enable", "--now", "hsaj-core.timer"], cwd=REPO_ROOT)


def set_env_var(lines: list[str], key: str, value: str) -> list[str]:
    prefix = f"{key}="
    replaced = False
    updated: list[str] = []
    for line in lines:
        if line.startswith(prefix):
            updated.append(f"{prefix}{value}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(f"{prefix}{value}")
    return updated


def set_env_var_if_blank(lines: list[str], key: str, value: str) -> list[str]:
    current = get_env_value(lines, key)
    if current:
        return lines
    return set_env_var(lines, key, value)


def get_env_value(lines: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            cleaned = line[len(prefix) :].strip()
            return cleaned or None
    return None


def run(command: list[str], *, cwd: Path) -> None:
    print(f"[run] {' '.join(command)}")
    subprocess.run(command, cwd=cwd, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
