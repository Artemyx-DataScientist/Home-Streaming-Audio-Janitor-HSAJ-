from __future__ import annotations

# ruff: noqa: B008
from pathlib import Path
from typing import Optional

import typer

from .config import ConfigError, LoadedConfig, find_config_path, load_config
from .db import database_status, init_database
from .scanner import scan_library

app = typer.Typer(help="Домашнее ядро HSAJ")
db_app = typer.Typer(help="Операции с БД")
app.add_typer(db_app, name="db")


def _load_config_or_exit(config_path: Optional[Path]) -> Path:
    try:
        return find_config_path(config_path)
    except ConfigError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


def _read_config_or_exit(config_path: Path) -> LoadedConfig:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


@db_app.command("init", help="Создать SQLite и прогнать миграции")
def db_init(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Путь к hsaj.yaml",
    ),  # noqa: B008
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)
    _, version = init_database(loaded.config.database)
    typer.echo(f"База готова. Текущая версия: {version or 'нет применённых миграций'}")


@db_app.command("status", help="Показать текущую версию схемы")
def db_status(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Путь к hsaj.yaml",
    ),  # noqa: B008
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)
    version = database_status(loaded.config.database)
    if version is None:
        typer.echo("База ещё не инициализирована или миграции не применялись.")
    else:
        typer.echo(f"Текущая версия схемы: {version}")


@app.command("scan", help="Просканировать директории библиотеки и обновить таблицу files")
def scan_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Путь к hsaj.yaml",
    ),  # noqa: B008
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Только вывод статистики без записи в БД",
    ),  # noqa: B008
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)
    if not loaded.config.paths.library_roots:
        typer.secho("В конфиге не заданы paths.library_roots", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)

    engine, _ = init_database(loaded.config.database)
    summary = scan_library(
        engine=engine,
        library_roots=loaded.config.paths.library_roots,
        dry_run=dry_run,
    )

    if dry_run:
        typer.echo(f"Найдено файлов: {summary.found_files}")
    else:
        typer.echo(
            "Сканирование завершено. "
            f"Новых: {summary.created}, обновлено: {summary.updated}, пропущено: {summary.skipped}"
        )


if __name__ == "__main__":
    app()
