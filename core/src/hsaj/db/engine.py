from __future__ import annotations

from pathlib import Path, PureWindowsPath

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from ..config import ConfigError, DatabaseConfig


def create_sqlalchemy_url(database_path: Path) -> str:
    resolved_path: Path = database_path.resolve()
    windows_view = PureWindowsPath(database_path)

    if resolved_path.drive or windows_view.drive:
        target = resolved_path if resolved_path.drive else windows_view
        return target.as_uri().replace("file://", "sqlite://")

    return f"sqlite:///{resolved_path.as_posix()}"


def build_engine(database: DatabaseConfig) -> Engine:
    """Создаёт SQLAlchemy Engine на основе настроек БД."""

    if database.driver != "sqlite":
        raise ConfigError("Поддерживается только SQLite")

    database_path = database.path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(create_sqlalchemy_url(database_path))
