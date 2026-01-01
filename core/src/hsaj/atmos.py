from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import ActionLog
from .db.models import File as FileModel

logger = logging.getLogger(__name__)


def ffprobe_json(path: Path) -> dict[str, Any]:
    """Возвращает JSON-структуру ffprobe для указанного файла."""

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("ffprobe не найден в PATH")
        return {}
    except Exception as exc:  # pragma: no cover - защитный блок
        logger.warning("Ошибка запуска ffprobe для %s: %s", path, exc)
        return {}

    if result.returncode != 0:
        stderr_tail = (result.stderr or "").strip()
        logger.warning("ffprobe вернул код %s для %s: %s", result.returncode, path, stderr_tail)
        return {}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("Не удалось распарсить вывод ffprobe для %s", path)
        return {}


def _value_contains_atmos(value: Any) -> bool:
    if isinstance(value, str):
        return "atmos" in value.casefold()
    if isinstance(value, (list, tuple)):
        return any(_value_contains_atmos(item) for item in value)
    return False


def _tags_contain_atmos(tags: Any) -> bool:
    if not isinstance(tags, dict):
        return False
    return any(_value_contains_atmos(value) for value in tags.values())


def is_atmos(path: Path) -> bool:
    """Определяет наличие Atmos в файле по профилю или тегам (регистр не важен)."""

    probe = ffprobe_json(path)

    streams = probe.get("streams", []) if isinstance(probe, dict) else []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        profile = stream.get("profile")
        if isinstance(profile, str) and "atmos" in profile.casefold():
            return True
        if _tags_contain_atmos(stream.get("tags")):
            return True

    format_section = probe.get("format") if isinstance(probe, dict) else None
    if isinstance(format_section, dict) and _tags_contain_atmos(format_section.get("tags")):
        return True

    return False


@dataclass(slots=True)
class AtmosMovePlan:
    file_id: int
    source: Path
    destination: Path
    artist: str | None
    album: str | None


_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\\\|?*]+')


def _sanitize_component(value: str | None, default: str) -> str:
    candidate = (value or "").strip() or default
    sanitized = _INVALID_WINDOWS_CHARS.sub("_", candidate).strip()
    return sanitized or default


def build_atmos_destination(file_record: FileModel, atmos_root: Path) -> Path:
    artist = _sanitize_component(file_record.artist, "Unknown Artist")
    album = _sanitize_component(file_record.album, "Unknown Album")
    return atmos_root / artist / album / Path(file_record.path).name


def _is_inside_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def plan_atmos_moves(
    session: Session,
    atmos_root: Path,
    detection_fn: Callable[[Path], bool] = is_atmos,
) -> list[AtmosMovePlan]:
    atmos_root = atmos_root.resolve()
    planned: list[AtmosMovePlan] = []

    files = session.execute(select(FileModel)).scalars().all()
    for file_record in files:
        source_path = Path(file_record.path)
        if not source_path.exists():
            logger.warning("Файл отсутствует на диске, пропускаем: %s", source_path)
            continue
        if _is_inside_root(source_path, atmos_root):
            continue
        if not detection_fn(source_path):
            continue

        destination = build_atmos_destination(file_record, atmos_root).resolve()
        if source_path.resolve() == destination:
            continue

        planned.append(
            AtmosMovePlan(
                file_id=file_record.id,
                source=source_path.resolve(),
                destination=destination,
                artist=file_record.artist,
                album=file_record.album,
            )
        )

    return planned


def apply_atmos_moves(
    session: Session,
    atmos_root: Path,
    detection_fn: Callable[[Path], bool] = is_atmos,
) -> list[AtmosMovePlan]:
    executed: list[AtmosMovePlan] = []
    moves = plan_atmos_moves(session=session, atmos_root=atmos_root, detection_fn=detection_fn)
    if not moves:
        return executed

    atmos_root.resolve().mkdir(parents=True, exist_ok=True)

    for move in moves:
        if not move.source.exists():
            logger.warning("Исходный файл не найден, пропускаем перемещение: %s", move.source)
            continue

        move.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move.source), move.destination)

        file_record = session.get(FileModel, move.file_id)
        if file_record is not None:
            file_record.path = str(move.destination)

        session.add(
            ActionLog(
                action="move_to_atmos",
                target_path=str(move.destination),
                details=json.dumps({"from": str(move.source)}),
            )
        )
        executed.append(move)

    session.commit()
    return executed
