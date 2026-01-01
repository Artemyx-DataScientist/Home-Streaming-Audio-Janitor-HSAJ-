from __future__ import annotations

from pathlib import Path, PureWindowsPath

from hsaj.db.engine import create_sqlalchemy_url


def test_create_sqlalchemy_url_windows_style_path() -> None:
    windows_path = PureWindowsPath("C:/music/hsaj.db")

    url = create_sqlalchemy_url(Path(windows_path))

    assert url == "sqlite:///C:/music/hsaj.db"


def test_create_sqlalchemy_url_unix_path(tmp_path: Path) -> None:
    db_path = tmp_path / "hsaj.db"

    url = create_sqlalchemy_url(db_path)

    assert url == f"sqlite:///{db_path.resolve().as_posix()}"
