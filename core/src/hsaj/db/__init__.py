from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy.engine import Engine

from ..config import ConfigError, DatabaseConfig
from .engine import build_engine
from .migrations import MIGRATIONS, Migration, apply_migrations, current_version

__all__ = ["init_database", "database_status", "build_engine"]


def init_database(
    database: DatabaseConfig,
    migrations: Iterable[Migration] | None = None,
) -> tuple[Engine, str | None]:
    """Создаёт (при необходимости) SQLite и прогоняет миграции."""

    if database.driver != "sqlite":
        raise ConfigError("Поддерживается только SQLite")

    engine = build_engine(database)
    version = apply_migrations(engine, list(migrations or MIGRATIONS))
    return engine, version


def database_status(database: DatabaseConfig) -> Optional[str]:
    """Возвращает текущую версию миграций для указанной БД."""

    if not database.path.exists():
        return None

    engine = build_engine(database)
    return current_version(engine)
