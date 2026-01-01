from __future__ import annotations

# ruff: noqa: B008
import asyncio
import os
from pathlib import Path
from typing import Optional

import typer
from sqlalchemy.orm import Session

from .atmos import apply_atmos_moves, plan_atmos_moves
from .config import ConfigError, LoadedConfig, find_config_path, load_config
from .db import database_status, init_database
from .scanner import scan_library
from .transport import DEFAULT_BRIDGE_WS_URL, TransportEventProcessor, listen_to_bridge

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


def _require_atmos_dir(loaded: LoadedConfig) -> Path:
    atmos_dir = loaded.config.paths.atmos_dir
    if atmos_dir is None:
        typer.secho("В конфиге не указан paths.atmos_dir", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1)
    return atmos_dir


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


@app.command("plan", help="Показать запланированные действия (перенос Atmos)")
def plan_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Путь к hsaj.yaml",
    ),  # noqa: B008
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)
    atmos_dir = _require_atmos_dir(loaded)

    engine, _ = init_database(loaded.config.database)
    with Session(engine) as session:
        planned_moves = plan_atmos_moves(session=session, atmos_root=atmos_dir)

    if not planned_moves:
        typer.echo("Atmos-файлы вне целевого каталога не найдены.")
        return

    typer.echo("Запланировано перемещение Atmos-файлов:")
    for move in planned_moves:
        typer.echo(f"- {move.source} -> {move.destination}")


@app.command("apply", help="Применить план (перенос Atmos в целевой каталог)")
def apply_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Путь к hsaj.yaml",
    ),  # noqa: B008
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)
    atmos_dir = _require_atmos_dir(loaded)

    engine, _ = init_database(loaded.config.database)
    with Session(engine) as session:
        executed_moves = apply_atmos_moves(session=session, atmos_root=atmos_dir)

    if not executed_moves:
        typer.echo("Перемещать нечего — план пуст.")
        return

    typer.echo(f"Перемещено файлов: {len(executed_moves)}")


@app.command("listen", help="Подключиться к bridge и собирать события воспроизведения")
def listen_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Путь к hsaj.yaml",
    ),  # noqa: B008
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)
    ws_url = os.environ.get("HSAJ_BRIDGE_WS", DEFAULT_BRIDGE_WS_URL)

    engine, _ = init_database(loaded.config.database)

    def _session_factory() -> Session:
        return Session(engine)

    processor = TransportEventProcessor(session_factory=_session_factory)

    try:
        asyncio.run(listen_to_bridge(ws_url=ws_url, processor=processor))
    except KeyboardInterrupt:
        typer.echo("Отключение от bridge по Ctrl+C")


if __name__ == "__main__":
    app()
