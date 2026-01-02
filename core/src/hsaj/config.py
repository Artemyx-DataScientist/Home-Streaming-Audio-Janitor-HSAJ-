from __future__ import annotations

import os
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
    """Ошибка чтения или валидации конфига HSAJ."""


class DatabaseConfig(BaseModel):
    """Настройки базы данных."""

    model_config = ConfigDict(extra="forbid")

    driver: str = Field(
        default="sqlite",
        description="Тип драйвера. Пока поддерживается только sqlite",
    )
    path: Path = Field(..., description="Путь к файлу БД или DSN")

    @field_validator("driver")
    @classmethod
    def ensure_supported_driver(cls, value: str) -> str:
        if value != "sqlite":
            msg = (
                "Поддерживается только драйвер 'sqlite'. "
                "Укажите driver: sqlite в конфиге hsaj.yaml."
            )
            raise ConfigError(msg)
        return value

    @field_validator("path")
    @classmethod
    def expand_user(cls, value: Path) -> Path:
        return value.expanduser()


class PathsConfig(BaseModel):
    """Пути, используемые ядром."""

    model_config = ConfigDict(extra="forbid")

    library_roots: list[Path] = Field(default_factory=list, description="Директории аудиотеки")
    quarantine_dir: Optional[Path] = Field(default=None, description="Путь для карантина удалений")
    atmos_dir: Optional[Path] = Field(default=None, description="Путь для Atmos файлов")
    inbox_dir: Optional[Path] = Field(default=None, description="Входящая директория")
    ffprobe_path: str = Field(default="ffprobe", description="Путь к бинарю ffprobe")

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

    @field_validator("ffprobe_path")
    @classmethod
    def normalize_ffprobe_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ConfigError("paths.ffprobe_path не может быть пустым")
        return str(Path(cleaned).expanduser())


class HsajConfig(BaseModel):
    """Корневой конфиг HSAJ."""

    model_config = ConfigDict(extra="forbid")

    database: DatabaseConfig
    paths: PathsConfig = Field(default_factory=PathsConfig)

    def resolve_relative_paths(self, base_path: Path) -> "HsajConfig":
        """Возвращает копию конфига с путями, разрешёнными относительно config файла."""

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

        ffprobe_candidate = Path(resolved_paths.ffprobe_path)
        if ffprobe_candidate.is_absolute():
            resolved_paths.ffprobe_path = str(ffprobe_candidate)
        elif ffprobe_candidate.parent != Path("."):
            resolved_paths.ffprobe_path = str((base_path / ffprobe_candidate).resolve())

        return self.model_copy(update={"database": resolved_db, "paths": resolved_paths})


@dataclass
class LoadedConfig:
    """Результат чтения конфига, включая путь к исходному файлу."""

    config: HsajConfig
    source_path: Path


def load_config(config_path: Path) -> LoadedConfig:
    """Читает YAML-конфиг и валидирует его через Pydantic."""

    if not config_path.exists():
        raise ConfigError(f"Конфиг не найден: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"Не удалось распарсить YAML: {exc}") from exc
    if raw is None:
        raise ConfigError("Файл конфига пустой или содержит только комментарии.")

    try:
        parsed = HsajConfig.model_validate(raw)
    except ValidationError as exc:
        human_errors = "; ".join(err["msg"] for err in exc.errors())
        raise ConfigError(f"Конфиг некорректен: {human_errors}") from exc
    except ConfigError:
        raise
    except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
        raise ConfigError(f"Неизвестная ошибка чтения конфига: {exc}") from exc

    return LoadedConfig(
        config=parsed.resolve_relative_paths(config_path.parent),
        source_path=config_path,
    )


def find_config_path(explicit: Optional[Path]) -> Path:
    """Ищет конфиг hsaj.yaml по приоритету: CLI → переменная окружения → дефолтные пути."""

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
        "Конфиг не найден. Передайте путь через --config, переменную окружения HSAJ_CONFIG "
        "или создайте configs/hsaj.yaml."
    )
