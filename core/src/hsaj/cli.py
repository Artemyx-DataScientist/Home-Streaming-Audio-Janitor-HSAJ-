# ruff: noqa: B008
import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .blocking import fetch_blocked_from_bridge, sync_blocked_objects
from .config import ConfigError, LoadedConfig, find_config_path, load_config
from .db import database_status, init_database
from .db.models import BlockCandidate, PlayHistory, RoonItemCache
from .executor import apply_plan, restore_from_quarantine
from .planner import build_plan
from .roon import BridgeClientError, cache_roon_track, fetch_track_from_bridge
from .scanner import scan_library
from .timeutils import utc_isoformat, utc_now
from .transport import DEFAULT_BRIDGE_WS_URL, TransportEventProcessor, listen_to_bridge

app = typer.Typer(help="HSAJ core CLI")
db_app = typer.Typer(help="Database operations")
roon_app = typer.Typer(help="Roon integration")
app.add_typer(db_app, name="db")
app.add_typer(roon_app, name="roon")


@dataclass(slots=True)
class TrackCacheWarmupResult:
    total: int = 0
    created: int = 0
    updated: int = 0
    failed: int = 0


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


def _warm_track_cache(
    session: Session,
    *,
    bridge_url: str | None,
) -> TrackCacheWarmupResult:
    track_ids = session.scalars(
        select(BlockCandidate.object_id)
        .where(
            BlockCandidate.object_type == "track",
            BlockCandidate.status == "planned",
        )
        .order_by(BlockCandidate.id.asc())
    ).all()
    unique_track_ids = list(dict.fromkeys(track_ids))

    result = TrackCacheWarmupResult(total=len(unique_track_ids))
    for track_id in unique_track_ids:
        try:
            track = fetch_track_from_bridge(track_id, base_url=bridge_url)
        except BridgeClientError:
            result.failed += 1
            continue
        existing = session.get(RoonItemCache, track.roon_track_id)
        cache_roon_track(session, track)
        if existing is None:
            result.created += 1
        else:
            result.updated += 1
    return result


@db_app.command("init", help="Create SQLite DB and apply migrations")
def db_init(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        is_flag=False,
        help="Path to hsaj.yaml",
    ),
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)
    _, version = init_database(loaded.config.database)
    typer.echo(f"Database ready. Current version: {version or 'no migrations applied'}")


@db_app.command("status", help="Show current schema version")
def db_status(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        is_flag=False,
        help="Path to hsaj.yaml",
    ),
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)
    version = database_status(loaded.config.database)
    if version is None:
        typer.echo("Database is not initialized yet or no migrations have been applied.")
    else:
        typer.echo(f"Current schema version: {version}")


@app.command("scan", help="Scan library roots and refresh the files table")
def scan_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        is_flag=False,
        help="Path to hsaj.yaml",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Only print statistics without writing to the DB",
    ),
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)
    if not loaded.config.paths.library_roots:
        typer.secho(
            "paths.library_roots is not configured",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    engine, _ = init_database(loaded.config.database)
    summary = scan_library(
        engine=engine,
        library_roots=loaded.config.paths.library_roots,
        allowed_extensions=loaded.config.paths.scan_extensions,
        excluded_dirs=loaded.config.paths.scan_exclude_dirs,
        batch_size=loaded.config.paths.scan_batch_size,
        dry_run=dry_run,
        ffprobe_path=loaded.config.paths.ffprobe_path,
    )

    if dry_run:
        typer.echo(f"Found files: {summary.found_files}")
    else:
        typer.echo(
            "Scan completed. "
            f"Created: {summary.created}, updated: {summary.updated}, skipped: {summary.skipped}"
        )


@app.command("plan", help="Show the current action plan")
def plan_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        is_flag=False,
        help="Path to hsaj.yaml",
    ),
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
        typer.echo("Plan is empty.")
        typer.echo(plan.to_json())
        return

    typer.echo("Action plan:")
    if plan.atmos_moves:
        typer.echo("Atmos moves:")
        for move in plan.atmos_moves:
            typer.echo(f"- file_id={move.file_id}: {move.source} -> {move.destination}")

    if plan.blocked_quarantine_due:
        typer.echo("Blocked quarantine due now:")
        for move in plan.blocked_quarantine_due:
            typer.echo(
                f"- candidate_id={move.candidate_id} file_id={move.file_id}: "
                f"{move.source} -> {move.destination} (reason={move.reason})"
            )

    if plan.blocked_quarantine_future:
        typer.echo("Blocked quarantine scheduled for later:")
        for move in plan.blocked_quarantine_future:
            typer.echo(
                f"- candidate_id={move.candidate_id} file_id={move.file_id}: "
                f"{move.source} -> {move.destination} (reason={move.reason})"
            )

    if plan.low_confidence:
        typer.echo("Low-confidence matches:")
        for item in plan.low_confidence:
            typer.echo(
                f"- candidate_id={item.candidate_id} {item.object_type}:{item.object_id} "
                f"files={item.matched_file_ids or '[]'} reason={item.reason}"
            )

    typer.echo("Plan JSON:")
    typer.echo(plan.to_json())


@app.command("apply", help="Apply the current plan")
def apply_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to hsaj.yaml",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Log the plan but do not move files",
    ),
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)

    engine, _ = init_database(loaded.config.database)
    with Session(engine) as session:
        plan = build_plan(session=session, config=loaded.config)
        result = apply_plan(
            session=session, config=loaded.config, plan=plan, dry_run=dry_run
        )

    if dry_run:
        typer.echo("dry_run: no files were changed; plan and dry_run entries were logged")
        return

    typer.echo(
        f"Applied. Atmos: {len(result.applied_atmos)}, quarantined: {len(result.quarantined)}, "
        f"skipped: {len(result.skipped)}"
    )


@app.command("history", help="Show recent play_history entries")
def history_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        is_flag=False,
        help="Path to hsaj.yaml",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        is_flag=False,
        min=1,
        help="Maximum number of entries to show",
    ),
    open_only: bool = typer.Option(
        False,
        "--open-only",
        help="Show only currently open playback entries",
    ),
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)

    engine, _ = init_database(loaded.config.database)
    with Session(engine) as session:
        query = select(PlayHistory).order_by(
            PlayHistory.started_at.desc(),
            PlayHistory.id.desc(),
        )
        if open_only:
            query = query.where(PlayHistory.ended_at.is_(None))
        entries = session.scalars(query.limit(limit)).all()

    if not entries:
        typer.echo("Play history is empty.")
        return

    for entry in entries:
        started_at = utc_isoformat(entry.started_at) or "unknown"
        ended_at = utc_isoformat(entry.ended_at) or "open"
        typer.echo(
            f"{started_at} -> {ended_at} "
            f"track={entry.track_id} source={entry.source} "
            f"quality={entry.quality or 'unknown'} played_ms={entry.played_ms or 0}"
        )


@app.command("listen", help="Connect to the bridge and collect playback events")
def listen_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        is_flag=False,
        help="Path to hsaj.yaml",
    ),
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
        typer.echo("Disconnected from bridge")


@roon_app.command("sync", help="Sync blocked objects from Roon and refresh candidates")
def roon_sync_command(
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        is_flag=False,
        help="Path to hsaj.yaml",
    ),
    bridge_url: Optional[str] = typer.Option(
        None,
        "--bridge-url",
        is_flag=False,
        help="Bridge HTTP URL (default: HSAJ_BRIDGE_HTTP or http://localhost:8080)",
    ),
    grace_days: Optional[int] = typer.Option(
        None,
        "--grace-days",
        is_flag=False,
        help="Days to wait after first seeing a block before action is due",
    ),
    cache_tracks: bool = typer.Option(
        False,
        "--cache-tracks",
        help="Warm RoonItemCache for planned track blocks via /track/{id}",
    ),
) -> None:
    resolved_path = _load_config_or_exit(config)
    loaded = _read_config_or_exit(resolved_path)

    try:
        blocked = fetch_blocked_from_bridge(base_url=bridge_url)
    except BridgeClientError as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    engine, _ = init_database(loaded.config.database)
    cache_result = TrackCacheWarmupResult()
    resolved_grace_days = (
        loaded.config.policy.block_grace_days
        if grace_days is None
        else grace_days
    )
    with Session(engine) as session:
        result = sync_blocked_objects(
            session=session,
            blocked_items=blocked,
            grace_period_days=resolved_grace_days,
            seen_at=utc_now(),
        )

        if cache_tracks:
            cache_result = _warm_track_cache(session=session, bridge_url=bridge_url)
        session.commit()

    typer.echo(
        "Blocked-object sync completed. "
        f"Raw blocks: {len(blocked)}; raw created: {result.raw_created}; "
        f"raw updated: {result.raw_updated}; candidates created: {result.candidates_created}; "
        f"candidates restored: {result.candidates_restored}"
    )
    if cache_tracks:
        typer.echo(
            "RoonItemCache warmup: "
            f"track ids: {cache_result.total}; created: {cache_result.created}; "
            f"updated: {cache_result.updated}; failed: {cache_result.failed}"
        )


@app.command("restore", help="Restore a file from quarantine by path or file_id")
def restore_command(
    target: str,
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        is_flag=False,
        help="Path to hsaj.yaml",
    ),
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
        typer.echo("Restore conflict: destination already exists, operation aborted")
        return
    if result.restored_path is None:
        typer.echo("Could not find a quarantine record or file to restore")
        return

    typer.echo(f"Restored file: {result.original_path}")


if __name__ == "__main__":
    app()
