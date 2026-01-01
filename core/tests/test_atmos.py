from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from hsaj.atmos import apply_atmos_moves, ffprobe_json, is_atmos, plan_atmos_moves
from hsaj.config import DatabaseConfig
from hsaj.db import init_database
from hsaj.db.models import ActionLog, File

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "tools" / "sample_ffprobe_outputs"


def _mock_run(stdout: str, returncode: int = 0, stderr: str | None = None) -> Callable[..., object]:
    def _runner(*_: object, **__: object) -> object:
        return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)

    return _runner


def test_ffprobe_json_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sample_output = (SAMPLES_DIR / "atmos_profile_eac3.json").read_text()
    monkeypatch.setattr("subprocess.run", _mock_run(sample_output))

    result = ffprobe_json(Path("dummy.mkv"))

    assert result["streams"][0]["profile"] == "E-AC-3 JOC (Dolby Atmos)"


def test_ffprobe_json_handles_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_: object, **__: object) -> object:
        raise FileNotFoundError("ffprobe missing")

    monkeypatch.setattr("subprocess.run", _raise)

    result = ffprobe_json(Path("dummy.mkv"))

    assert result == {}


def test_ffprobe_json_handles_invalid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("subprocess.run", _mock_run("not-a-json"))

    result = ffprobe_json(Path("dummy.mkv"))

    assert result == {}


def test_is_atmos_detects_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    sample = json.loads((SAMPLES_DIR / "atmos_profile_eac3.json").read_text())
    monkeypatch.setattr("hsaj.atmos.ffprobe_json", lambda _: sample)

    assert is_atmos(Path("demo.mkv")) is True


def test_is_atmos_detects_tags_and_skips_plain_truehd(monkeypatch: pytest.MonkeyPatch) -> None:
    tagged = json.loads((SAMPLES_DIR / "atmos_stream_tag.json").read_text())
    no_atmos = json.loads((SAMPLES_DIR / "truehd_no_atmos.json").read_text())

    monkeypatch.setattr("hsaj.atmos.ffprobe_json", lambda _: tagged)
    assert is_atmos(Path("tagged.mkv")) is True

    monkeypatch.setattr("hsaj.atmos.ffprobe_json", lambda _: no_atmos)
    assert is_atmos(Path("plain_truehd.mkv")) is False


def test_is_atmos_checks_format_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    sample = json.loads((SAMPLES_DIR / "atmos_format_tag.json").read_text())
    monkeypatch.setattr("hsaj.atmos.ffprobe_json", lambda _: sample)

    assert is_atmos(Path("format_tag.flac")) is True


def _prepare_db(tmp_path: Path) -> Engine:
    db_path = tmp_path / "hsaj.db"
    engine, _ = init_database(DatabaseConfig(driver="sqlite", path=db_path))
    return engine


def test_plan_and_apply_moves_atmos_files(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    library_root.mkdir()
    atmos_root = tmp_path / "atmos"

    source_path = library_root / "Artist" / "Album" / "track.flac"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("audio")

    engine = _prepare_db(tmp_path)
    with Session(engine) as session:
        session.add(
            File(
                path=str(source_path.resolve()),
                size_bytes=123,
                format="flac",
                mtime=None,
                artist="Artist",
                album="Album",
                title="Track",
                track_number=1,
                year=2024,
                duration_seconds=300,
            )
        )
        session.commit()

    detection_calls = 0

    def detection(path: Path) -> bool:
        nonlocal detection_calls
        detection_calls += 1
        return path == source_path.resolve()

    with Session(engine) as check_session:
        plan = plan_atmos_moves(check_session, atmos_root=atmos_root, detection_fn=detection)
        assert len(plan) == 1
        assert plan[0].destination == atmos_root / "Artist" / "Album" / "track.flac"

    with Session(engine) as apply_session:
        applied = apply_atmos_moves(apply_session, atmos_root=atmos_root, detection_fn=detection)
        assert len(applied) == 1

    destination = atmos_root / "Artist" / "Album" / "track.flac"
    assert destination.exists()
    assert not source_path.exists()
    assert detection_calls == 2

    with Session(engine) as verify_session:
        stored_file = verify_session.execute(select(File)).scalar_one()
        assert stored_file.path == str(destination.resolve())

        logs = verify_session.execute(select(ActionLog)).scalars().all()
        assert len(logs) == 1
        assert "track.flac" in logs[0].target_path

    with Session(engine) as second_run_session:
        second_apply = apply_atmos_moves(
            second_run_session,
            atmos_root=atmos_root,
            detection_fn=detection,
        )
        assert second_apply == []
        assert detection_calls == 2

    with Session(engine) as final_check_session:
        final_logs = final_check_session.execute(select(ActionLog)).scalars().all()
        assert len(final_logs) == 1


def test_plan_uses_unknown_fallbacks(tmp_path: Path) -> None:
    atmos_root = tmp_path / "atmos"
    source = tmp_path / "misc.flac"
    source.write_text("dummy")

    engine = _prepare_db(tmp_path)
    with Session(engine) as session:
        session.add(
            File(
                path=str(source),
                size_bytes=10,
                format="flac",
                mtime=None,
                artist=None,
                album=None,
                title=None,
                track_number=None,
                year=None,
                duration_seconds=None,
            )
        )
        session.commit()

    with Session(engine) as plan_session:
        moves = plan_atmos_moves(
            plan_session,
            atmos_root=atmos_root,
            detection_fn=lambda _: True,
        )

    assert moves[0].destination == atmos_root / "Unknown Artist" / "Unknown Album" / "misc.flac"
