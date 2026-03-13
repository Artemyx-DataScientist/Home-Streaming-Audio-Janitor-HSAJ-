from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_CONFIG_CANDIDATES = (
    Path("configs/hsaj.yaml"),
    Path("hsaj.yaml"),
)


class ConfigError(Exception):
    """Raised when reading or validating HSAJ config fails."""


class DatabaseConfig(BaseModel):
    """Database settings."""

    model_config = ConfigDict(extra="forbid")

    driver: str = Field(
        default="sqlite",
        description="Database driver. Only sqlite is supported right now.",
    )
    path: Path = Field(..., description="Path to the database file or DSN.")

    @field_validator("driver")
    @classmethod
    def ensure_supported_driver(cls, value: str) -> str:
        if value != "sqlite":
            msg = "Only the 'sqlite' driver is supported. Set driver: sqlite in hsaj.yaml."
            raise ConfigError(msg)
        return value

    @field_validator("path")
    @classmethod
    def expand_user(cls, value: Path) -> Path:
        return value.expanduser()


class PathsConfig(BaseModel):
    """Filesystem paths and scanner settings used by the core."""

    model_config = ConfigDict(extra="forbid")

    library_roots: list[Path] = Field(
        default_factory=list,
        description="Library root directories.",
    )
    quarantine_dir: Optional[Path] = Field(
        default=None,
        description="Directory used for quarantine moves.",
    )
    atmos_dir: Optional[Path] = Field(
        default=None,
        description="Directory used for Atmos files.",
    )
    inbox_dir: Optional[Path] = Field(
        default=None,
        description="Inbox directory.",
    )
    scan_extensions: list[str] = Field(
        default_factory=lambda: [
            "aac",
            "aiff",
            "alac",
            "dsf",
            "flac",
            "m4a",
            "mp3",
            "ogg",
            "opus",
            "wav",
            "wma",
        ],
        description="Allowed file extensions for the scanner.",
    )
    scan_exclude_dirs: list[Path] = Field(
        default_factory=list,
        description="Directories that the scanner should skip.",
    )
    scan_batch_size: int = Field(
        default=200,
        ge=1,
        description="Number of scanned files between DB commits.",
    )
    ffprobe_path: str = Field(
        default="ffprobe",
        description="Path to the ffprobe binary.",
    )

    @field_validator("library_roots", mode="before")
    @classmethod
    def ensure_list(cls, value: Any) -> list[Path]:
        if value is None:
            return []
        if isinstance(value, (str, Path)):
            return [Path(value)]
        return [Path(item) for item in value]

    @field_validator("quarantine_dir", "atmos_dir", "inbox_dir", mode="before")
    @classmethod
    def normalize_path(cls, value: Any) -> Any:
        if value is None:
            return None
        return Path(value)

    @field_validator("scan_extensions", mode="before")
    @classmethod
    def normalize_scan_extensions(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    @field_validator("scan_exclude_dirs", mode="before")
    @classmethod
    def normalize_scan_exclude_dirs(cls, value: Any) -> list[Path]:
        if value is None:
            return []
        if isinstance(value, (str, Path)):
            return [Path(value)]
        return [Path(item) for item in value]

    @field_validator("ffprobe_path")
    @classmethod
    def normalize_ffprobe_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ConfigError("paths.ffprobe_path must not be empty")
        return str(Path(cleaned).expanduser())


class PolicyConfig(BaseModel):
    """Domain policy settings used by planning and cleanup commands."""

    model_config = ConfigDict(extra="forbid")

    block_grace_days: int = Field(
        default=30,
        ge=0,
        description="Days to wait after first seeing a block before quarantine is due.",
    )
    quarantine_delete_days: int = Field(
        default=60,
        ge=0,
        description="Days to keep files in quarantine before optional hard delete.",
    )
    auto_delete: bool = Field(
        default=False,
        description="Whether files should be deleted automatically after quarantine expiry.",
    )
    allow_hard_delete: bool = Field(
        default=False,
        description="Explicit acknowledgement gate required before auto_delete may remove files.",
    )
    enable_behavior_scoring: bool = Field(
        default=True,
        description="Reserved flag for future soft-scoring heuristics.",
    )
    soft_never_played_days: int = Field(
        default=180,
        ge=0,
        description="Age threshold for advisory never-played candidates.",
    )
    soft_inbox_days: int = Field(
        default=30,
        ge=0,
        description="Age threshold for advisory inbox cleanup candidates.",
    )


class BridgeConfig(BaseModel):
    """Bridge connectivity and contract settings."""

    model_config = ConfigDict(extra="forbid")

    http_url: str = Field(
        default="http://localhost:8080",
        description="Base HTTP URL for the bridge.",
    )
    ws_url: str = Field(
        default="ws://localhost:8080/events",
        description="WebSocket URL for bridge transport events.",
    )
    contract_version: str = Field(
        default="v2",
        description="Expected blocked snapshot contract version.",
    )
    required_source_mode: str | None = Field(
        default=None,
        description="Optional blocked snapshot source mode required for readiness and destructive flows.",
    )
    max_blocked_sync_age_minutes: int | None = Field(
        default=None,
        ge=1,
        description="Optional maximum age for the last successful blocked sync before readiness fails.",
    )

    @field_validator("required_source_mode")
    @classmethod
    def normalize_required_source_mode(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class SecurityConfig(BaseModel):
    """Security settings for operator-facing surfaces."""

    model_config = ConfigDict(extra="forbid")

    operator_host: str = Field(
        default="127.0.0.1",
        description="Bind host for the core operator API.",
    )
    operator_port: int = Field(
        default=8090,
        ge=1,
        le=65535,
        description="Bind port for the core operator API.",
    )
    operator_token: str | None = Field(
        default=None,
        description="Optional shared token required for operator API requests.",
    )


class ObservabilityConfig(BaseModel):
    """Logging and diagnostics settings."""

    model_config = ConfigDict(extra="forbid")

    log_level: str = Field(default="INFO", description="Application log level.")
    structured_logging: bool = Field(
        default=True,
        description="Emit structured JSON logs where available.",
    )
    service_name: str = Field(
        default="hsaj-core",
        description="Service name included in structured logs and metrics.",
    )


class RuntimeConfig(BaseModel):
    """Background runtime settings for scheduled core jobs."""

    model_config = ConfigDict(extra="forbid")

    enable_background_jobs: bool = Field(
        default=False,
        description="Run background blocked-sync and cleanup jobs inside hsaj serve.",
    )
    blocked_sync_interval_minutes: int = Field(
        default=15,
        ge=1,
        description="How often to refresh blocked objects from the bridge.",
    )
    cleanup_interval_minutes: int = Field(
        default=60,
        ge=1,
        description="How often to run quarantine retention cleanup.",
    )
    blocked_sync_on_start: bool = Field(
        default=True,
        description="Run blocked sync once when the runtime scheduler starts.",
    )
    cleanup_on_start: bool = Field(
        default=True,
        description="Run cleanup once when the runtime scheduler starts.",
    )


class HsajConfig(BaseModel):
    """Root HSAJ config model."""

    model_config = ConfigDict(extra="forbid")

    database: DatabaseConfig
    paths: PathsConfig = Field(default_factory=PathsConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    bridge: BridgeConfig = Field(default_factory=BridgeConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    def resolve_relative_paths(self, base_path: Path) -> "HsajConfig":
        """Return a copy with paths resolved relative to the config file."""

        def _resolve(path_value: Optional[Path]) -> Optional[Path]:
            if path_value is None:
                return None
            return path_value if path_value.is_absolute() else (base_path / path_value).resolve()

        resolved_db = self.database.model_copy()
        resolved_db.path = _resolve(self.database.path)  # type: ignore[assignment]

        resolved_paths = self.paths.model_copy()
        resolved_paths.library_roots = [
            root if root.is_absolute() else (base_path / root).resolve()
            for root in resolved_paths.library_roots
        ]
        resolved_paths.quarantine_dir = _resolve(resolved_paths.quarantine_dir)
        resolved_paths.atmos_dir = _resolve(resolved_paths.atmos_dir)
        resolved_paths.inbox_dir = _resolve(resolved_paths.inbox_dir)
        resolved_paths.scan_exclude_dirs = [
            excluded if excluded.is_absolute() else (base_path / excluded).resolve()
            for excluded in resolved_paths.scan_exclude_dirs
        ]

        ffprobe_candidate = Path(resolved_paths.ffprobe_path)
        if ffprobe_candidate.is_absolute():
            resolved_paths.ffprobe_path = str(ffprobe_candidate)
        elif ffprobe_candidate.parent != Path("."):
            resolved_paths.ffprobe_path = str((base_path / ffprobe_candidate).resolve())

        return self.model_copy(update={"database": resolved_db, "paths": resolved_paths})

    def ffprobe_resolved_path(self) -> Path | None:
        candidate = Path(self.paths.ffprobe_path)
        if candidate.is_absolute():
            return candidate
        resolved = shutil.which(self.paths.ffprobe_path)
        return Path(resolved) if resolved else None


@dataclass
class LoadedConfig:
    """Loaded config paired with its source path."""

    config: HsajConfig
    source_path: Path


def load_config(config_path: Path) -> LoadedConfig:
    """Read YAML config and validate it with Pydantic."""

    if not config_path.exists():
        raise ConfigError(f"Config not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse YAML: {exc}") from exc
    if raw is None:
        raise ConfigError("Config file is empty or contains comments only.")

    try:
        parsed = HsajConfig.model_validate(raw)
    except ValidationError as exc:
        human_errors = "; ".join(err["msg"] for err in exc.errors())
        raise ConfigError(f"Invalid config: {human_errors}") from exc
    except ConfigError:
        raise
    except Exception as exc:  # pragma: no cover - defensive wrapper
        raise ConfigError(f"Unexpected config error: {exc}") from exc

    resolved = parsed.resolve_relative_paths(config_path.parent)

    operator_token = os.environ.get("HSAJ_OPERATOR_TOKEN")
    if operator_token is not None:
        cleaned = operator_token.strip()
        resolved.security.operator_token = cleaned or None

    return LoadedConfig(config=resolved, source_path=config_path)


def find_config_path(explicit: Optional[Path]) -> Path:
    """Locate hsaj.yaml using CLI, env vars, then default paths."""

    if explicit is not None:
        return explicit

    env_value: str | None = None
    for name in ("HSAJ_CONFIG", "HSAJ_CONFIG_PATH"):
        env_value = env_value or os.environ.get(name)
    if env_value:
        return Path(env_value)

    for candidate in DEFAULT_CONFIG_CANDIDATES:
        if candidate.exists():
            return candidate

    raise ConfigError(
        "Config not found. Pass --config, set HSAJ_CONFIG, or create configs/hsaj.yaml."
    )
