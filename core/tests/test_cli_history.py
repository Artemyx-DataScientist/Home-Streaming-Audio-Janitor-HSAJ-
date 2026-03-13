from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from hsaj.cli import history_command
from hsaj.config import DatabaseConfig
from hsaj.db import init_database
from hsaj.db.models import PlayHistory


def test_history_command_shows_recent_entries(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "hsaj.yaml"
    db_path = tmp_path / "hsaj.db"
    config_path.write_text(
        f"""
database:
  driver: sqlite
  path: {json.dumps(str(db_path))}
paths:
  library_roots: []
""".strip(),
        encoding="utf-8",
    )

    engine, _ = init_database(DatabaseConfig(driver="sqlite", path=db_path))
    with Session(engine) as session:
        session.add(
            PlayHistory(
                track_id="track-1",
                source="bridge",
                quality="lossless",
                started_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                ended_at=datetime(2024, 1, 1, 12, 3, 0, tzinfo=timezone.utc),
                played_ms=180_000,
                title="Title",
                artist="Artist",
                album="Album",
            )
        )
        session.commit()

    history_command(config=config_path, limit=5, open_only=False)

    captured = capsys.readouterr()
    assert "track=track-1" in captured.out
    assert "quality=lossless" in captured.out
    assert "played_ms=180000" in captured.out
