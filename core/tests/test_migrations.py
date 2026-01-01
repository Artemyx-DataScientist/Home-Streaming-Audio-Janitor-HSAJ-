from __future__ import annotations

from pathlib import Path

from hsaj.config import DatabaseConfig
from hsaj.db import database_status, init_database


def test_db_init_and_status(tmp_path: Path) -> None:
    db_path = tmp_path / "hsaj.db"
    db_config = DatabaseConfig(driver="sqlite", path=db_path)

    engine, version = init_database(db_config)

    assert engine is not None
    assert version == "0002_play_history"

    status = database_status(db_config)
    assert status == "0002_play_history"
