from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from ..config import ConfigError, DatabaseConfig


def create_sqlalchemy_url(database_path: Path) -> str:
    return f"sqlite:///{database_path}"


def build_engine(database: DatabaseConfig) -> Engine:
    """Создаёт SQLAlchemy Engine на основе настроек БД."""

    if database.driver != "sqlite":
        raise ConfigError("Поддерживается только SQLite")

    database_path = database.path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(create_sqlalchemy_url(database_path))
