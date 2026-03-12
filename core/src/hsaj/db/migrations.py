from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from .models import (
    Base,
    BlockCandidate,
    BridgeSyncStatus,
    Exemption,
    LibraryAlbum,
    LibraryArtist,
    LibraryTrack,
    PlanRun,
    PlayHistory,
    ReviewDecision,
    RoonBlockRaw,
    RoonItemCache,
    RuntimeJobStatus,
)

MigrationCallable = Callable[[Connection], None]


@dataclass(frozen=True)
class Migration:
    """Описание миграции."""

    version: str
    description: str
    upgrade: MigrationCallable


def _ensure_version_table(conn: Connection) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS hsaj_migrations (
                version TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
    )


def _is_applied(conn: Connection, version: str) -> bool:
    result = conn.execute(
        text("SELECT 1 FROM hsaj_migrations WHERE version = :version"),
        {"version": version},
    )
    return result.first() is not None


def _mark_applied(conn: Connection, migration: Migration) -> None:
    conn.execute(
        text(
            """
            INSERT INTO hsaj_migrations (version, description, applied_at)
            VALUES (:version, :description, :applied_at)
            """
        ),
        {
            "version": migration.version,
            "description": migration.description,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _migration_v1(conn: Connection) -> None:
    Base.metadata.create_all(bind=conn)


def _migration_v2(conn: Connection) -> None:
    PlayHistory.__table__.create(bind=conn, checkfirst=True)


def _migration_v3(conn: Connection) -> None:
    RoonItemCache.__table__.create(bind=conn, checkfirst=True)


def _migration_v4(conn: Connection) -> None:
    RoonBlockRaw.__table__.create(bind=conn, checkfirst=True)
    BlockCandidate.__table__.create(bind=conn, checkfirst=True)


def _migration_v5(conn: Connection) -> None:
    existing_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(files)")).fetchall()}
    if "atmos_detected" in existing_columns:
        return
    conn.execute(
        text(
            """
            ALTER TABLE files
            ADD COLUMN atmos_detected BOOLEAN NOT NULL DEFAULT 0
            """
        )
    )


def _migration_v6(conn: Connection) -> None:
    raw_columns = {
        row[1] for row in conn.execute(text("PRAGMA table_info(roon_blocks_raw)")).fetchall()
    }
    candidate_columns = {
        row[1] for row in conn.execute(text("PRAGMA table_info(block_candidates)")).fetchall()
    }

    if "metadata_json" not in raw_columns:
        conn.execute(
            text(
                """
                ALTER TABLE roon_blocks_raw
                ADD COLUMN metadata_json TEXT NULL
                """
            )
        )
    if "metadata_json" not in candidate_columns:
        conn.execute(
            text(
                """
                ALTER TABLE block_candidates
                ADD COLUMN metadata_json TEXT NULL
                """
            )
        )


def _migration_v7(conn: Connection) -> None:
    action_columns = {
        row[1] for row in conn.execute(text("PRAGMA table_info(actions_log)")).fetchall()
    }
    candidate_columns = {
        row[1] for row in conn.execute(text("PRAGMA table_info(block_candidates)")).fetchall()
    }

    if "request_id" not in action_columns:
        conn.execute(
            text(
                """
                ALTER TABLE actions_log
                ADD COLUMN request_id TEXT NULL
                """
            )
        )
    if "plan_id" not in action_columns:
        conn.execute(
            text(
                """
                ALTER TABLE actions_log
                ADD COLUMN plan_id TEXT NULL
                """
            )
        )

    additions = {
        "source": (
            """
            ALTER TABLE block_candidates
            ADD COLUMN source TEXT NOT NULL DEFAULT 'bridge.blocked.v1'
            """
        ),
        "rule_id": (
            """
            ALTER TABLE block_candidates
            ADD COLUMN rule_id TEXT NOT NULL DEFAULT ''
            """
        ),
        "explanation_json": (
            """
            ALTER TABLE block_candidates
            ADD COLUMN explanation_json TEXT NULL
            """
        ),
        "delete_after": (
            """
            ALTER TABLE block_candidates
            ADD COLUMN delete_after DATETIME NULL
            """
        ),
        "last_transition_at": (
            """
            ALTER TABLE block_candidates
            ADD COLUMN last_transition_at DATETIME NULL
            """
        ),
    }
    for column_name, statement in additions.items():
        if column_name in candidate_columns:
            continue
        conn.execute(text(statement))

    Exemption.__table__.create(bind=conn, checkfirst=True)
    PlanRun.__table__.create(bind=conn, checkfirst=True)


def _migration_v8(conn: Connection) -> None:
    LibraryArtist.__table__.create(bind=conn, checkfirst=True)
    LibraryAlbum.__table__.create(bind=conn, checkfirst=True)
    LibraryTrack.__table__.create(bind=conn, checkfirst=True)


def _migration_v9(conn: Connection) -> None:
    ReviewDecision.__table__.create(bind=conn, checkfirst=True)


def _migration_v10(conn: Connection) -> None:
    BridgeSyncStatus.__table__.create(bind=conn, checkfirst=True)


def _migration_v11(conn: Connection) -> None:
    RuntimeJobStatus.__table__.create(bind=conn, checkfirst=True)


MIGRATIONS: list[Migration] = [
    Migration(
        version="0001_initial",
        description="Создание базовых таблиц",
        upgrade=_migration_v1,
    ),
    Migration(
        version="0002_play_history",
        description="Добавление таблицы play_history",
        upgrade=_migration_v2,
    ),
    Migration(
        version="0003_roon_items_cache",
        description="Добавление таблицы roon_items_cache",
        upgrade=_migration_v3,
    ),
    Migration(
        version="0004_blocking_pipeline",
        description="Добавление таблиц roon_blocks_raw и block_candidates",
        upgrade=_migration_v4,
    ),
    Migration(
        version="0005_atmos_detected",
        description="Добавление устойчивого Atmos-флага для files",
        upgrade=_migration_v5,
    ),
    Migration(
        version="0006_block_metadata",
        description="Add metadata_json to roon_blocks_raw and block_candidates",
        upgrade=_migration_v6,
    ),
    Migration(
        version="0007_operator_and_retention_foundations",
        description="Add exemptions, plan_runs, retention, and structured action metadata",
        upgrade=_migration_v7,
    ),
    Migration(
        version="0008_normalized_library_graph",
        description="Add normalized artist, album, and track graph tables",
        upgrade=_migration_v8,
    ),
    Migration(
        version="0009_review_decisions",
        description="Add persistent operator review decisions for advisory candidates",
        upgrade=_migration_v9,
    ),
    Migration(
        version="0010_bridge_sync_status",
        description="Add persisted bridge sync freshness and status tracking",
        upgrade=_migration_v10,
    ),
    Migration(
        version="0011_runtime_job_status",
        description="Add persisted runtime job status tracking for background scheduler",
        upgrade=_migration_v11,
    ),
]


def apply_migrations(engine: Engine, migrations: Iterable[Migration] | None = None) -> str | None:
    """Применяет миграции по порядку и возвращает последнюю применённую версию."""

    migration_plan = list(migrations or MIGRATIONS)
    if not migration_plan:
        return None

    with engine.begin() as conn:
        _ensure_version_table(conn)
        for migration in migration_plan:
            if _is_applied(conn, migration.version):
                continue
            migration.upgrade(conn)
            _mark_applied(conn, migration)

        last_version = conn.execute(
            text("SELECT version FROM hsaj_migrations ORDER BY applied_at DESC LIMIT 1")
        ).scalar_one_or_none()

    return last_version


def current_version(engine: Engine) -> str | None:
    """Возвращает текущую версию схемы или None, если миграции не применялись."""

    with engine.begin() as conn:
        _ensure_version_table(conn)
        result = conn.execute(
            text("SELECT version FROM hsaj_migrations ORDER BY applied_at DESC LIMIT 1")
        ).scalar_one_or_none()
        return result
