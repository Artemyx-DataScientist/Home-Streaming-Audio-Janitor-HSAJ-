from __future__ import annotations

# ruff: noqa: B008
import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from sqlalchemy.orm import Session

from .blocking import (
    BLOCK_GRACE_DAYS_DEFAULT,
    fetch_blocked_from_bridge,
    sync_blocked_objects,
)
from .config import ConfigError, LoadedConfig, find_config_path, load_config
from .db import database_status, init_database
from .executor import apply_plan, restore_from_quarantine
from .planner import build_plan
from .roon import BridgeClientError
from .scanner import scan_library
from .transport import DEFAULT_BRIDGE_WS_URL, TransportEventProcessor, listen_to_bridge

app = typer.Typer(help="Домашнее ядро HSAJ")
db_app = typer.Typer(help="Операции с БД")
roon_app = typer.Typer(help="Интеграция с Roon")
app.add_typer(db_app, name="db")
app.add_typer(roon_app, name="roon")


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

    engine, _ = init_database(loaded.config.database)
    with Session(engine) as session:
        plan = build_plan(session=session, config=loaded.config)

    if not any(
        (
            plan.atmos_moves,
            plan.blocked_quarantine_due,
            plan.blocked_quarantine_future,
            plan.low_confidence,
        )
    ):
        typer.echo("План пуст — нечего делать.")
        typer.echo(plan.to_json())
        return

    typer.echo("План действий:")
    if plan.atmos_moves:
        typer.echo("Atmos (перемещение):")
        for move in plan.atmos_moves:
            typer.echo(f"- file_id={move.file_id}: {move.source} -> {move.destination}")

    if plan.blocked_quarantine_due:
        typer.echo("Карантин (срок наступил):")
        for move in plan.blocked_quarantine_due:
            typer.echo(
                f"- candidate_id={move.candidate_id} file_id={move.file_id}: "
                f"{move.source} -> {move.destination} (reason={move.reason})"
            )

    if plan.blocked_quarantine_future:
        typer.echo("Карантин (ещё не наступил):")
        for move in plan.blocked_quarantine_future:
            typer.echo(
                f"- candidate_id={move.candidate_id} file_id={move.file_id}: "
                f"{move.source} -> {move.destination} (reason={move.reason})"
            )

    if plan.low_confidence:
        typer.echo("Низкая уверенность (нужно вмешательство):")
        for item in plan.low_confidence:
            typer.echo(
                f"- candidate_id={item.candidate_id} {item.object_type}:{item.object_id} "
                f"files={item.matched_file_ids or '[]'} reason={item.reason}"
            )

    typer.echo("JSON-представление плана:")
    typer.echo(plan.to_json())


@app.command("apply", help="Применить план (перенос Atmos в целевой каталог)")
def apply_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Путь к hsaj.yaml",
    ),  # noqa: B008
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Только логировать действия без изменений",
    ),  # noqa: B008
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)

    engine, _ = init_database(loaded.config.database)
    with Session(engine) as session:
        plan = build_plan(session=session, config=loaded.config)
        result = apply_plan(session=session, config=loaded.config, plan=plan, dry_run=dry_run)

    if dry_run:
        typer.echo("dry_run: действия не выполнялись, записана строчка в actions_log")
        return

    typer.echo(
        f"Применено. Atmos: {len(result.applied_atmos)}, в карантин: {len(result.quarantined)}, "
        f"пропущено: {len(result.skipped)}"
    )


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


@roon_app.command("sync", help="Синхронизировать блоки из Roon и обновить кандидатов")
def roon_sync_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Путь к hsaj.yaml",
    ),  # noqa: B008
    bridge_url: Optional[str] = typer.Option(
        None,
        "--bridge-url",
        help="HTTP URL bridge (по умолчанию переменные окружения или http://localhost:8080)",
    ),  # noqa: B008
    grace_days: int = typer.Option(
        BLOCK_GRACE_DAYS_DEFAULT,
        "--grace-days",
        help="Сколько дней ждать после первого обнаружения блока",
    ),  # noqa: B008
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)

    try:
        blocked = fetch_blocked_from_bridge(base_url=bridge_url)
    except BridgeClientError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    engine, _ = init_database(loaded.config.database)
    with Session(engine) as session:
        result = sync_blocked_objects(
            session=session,
            blocked_items=blocked,
            grace_period_days=grace_days,
            seen_at=datetime.now(timezone.utc),
        )
        session.commit()

    typer.echo(
        "Синхронизация блоков завершена. "
        f"Сырых блоков: {len(blocked)}; создано raw: {result.raw_created}; "
        f"обновлено raw: {result.raw_updated}; новые кандидаты: {result.candidates_created}; "
        f"снято блоков: {result.candidates_restored}"
    )


@app.command("restore", help="Восстановить файл из карантина по пути или file_id")
def restore_command(
    target: str,
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Путь к hsaj.yaml",
    ),  # noqa: B008
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)

    try:
        file_id = int(target)
        target_path: Path | int = file_id
    except ValueError:
        target_path = Path(target)

    engine, _ = init_database(loaded.config.database)
    with Session(engine) as session:
        result = restore_from_quarantine(session=session, target=target_path)

    if result.conflict:
        typer.echo("Конфликт восстановления: целевой путь уже существует, операция отменена")
        return
    if result.restored_path is None:
        typer.echo("Не удалось найти запись о карантине или файл, восстановление не выполнено")
        return

    typer.echo(f"Файл восстановлен: {result.original_path}")


if __name__ == "__main__":
    app()
