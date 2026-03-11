from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mutagen.id3 import ID3, TALB, TDRC, TIT2, TPE1, TRCK
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from hsaj.config import DatabaseConfig
from hsaj.db import init_database
from hsaj.db.models import File, LibraryAlbum, LibraryArtist, LibraryTrack
from hsaj.scanner import scan_library


def _prepare_db(tmp_path: Path) -> Engine:
    db_path = tmp_path / "hsaj.db"
    database = DatabaseConfig(driver="sqlite", path=db_path)
    engine, _ = init_database(database)
    return engine


def _create_id3_file(path: Path) -> None:
    path.write_bytes(b"")
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Test Title"))
    tags.add(TPE1(encoding=3, text="Test Artist"))
    tags.add(TALB(encoding=3, text="Test Album"))
    tags.add(TRCK(encoding=3, text="1"))
    tags.add(TDRC(encoding=3, text="2024"))
    tags.save(path)


def test_scan_creates_and_updates_file(tmp_path: Path) -> None:
    engine = _prepare_db(tmp_path)
    library_root = tmp_path / "library"
    library_root.mkdir()
    file_path = library_root / "track.mp3"
    _create_id3_file(file_path)

    summary_first = scan_library(
        engine=engine,
        library_roots=[library_root],
        dry_run=False,
        atmos_detection_fn=lambda _: False,
    )

    assert summary_first.found_files == 1
    assert summary_first.created == 1
    with Session(engine) as session:
        stored = session.execute(select(File)).scalar_one()
        assert stored.path == str(file_path.resolve())
        assert stored.artist == "Test Artist"
        assert stored.album == "Test Album"
        assert stored.title == "Test Title"
        assert stored.track_number == 1
        assert stored.year == 2024
        assert stored.atmos_detected is False
        first_mtime = stored.mtime

    new_mtime = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    new_epoch = new_mtime.timestamp()
    file_path.write_bytes(b"updated content")
    os.utime(file_path, (new_epoch, new_epoch))

    summary_second = scan_library(
        engine=engine,
        library_roots=[library_root],
        dry_run=False,
        atmos_detection_fn=lambda _: False,
    )

    assert summary_second.found_files == 1
    assert summary_second.updated == 1
    with Session(engine) as session:
        updated = session.execute(select(File)).scalar_one()
        assert updated.mtime > first_mtime


def test_scan_handles_file_without_tags_when_extension_is_allowed(tmp_path: Path) -> None:
    engine = _prepare_db(tmp_path)
    library_root = tmp_path / "library"
    library_root.mkdir()
    file_path = library_root / "no_tags.bin"
    file_path.write_text("content")

    summary = scan_library(
        engine=engine,
        library_roots=[library_root],
        allowed_extensions=["bin"],
        dry_run=False,
        atmos_detection_fn=lambda _: False,
    )

    assert summary.found_files == 1
    assert summary.created == 1
    with Session(engine) as session:
        stored = session.execute(select(File)).scalar_one()
        assert stored.artist is None
        assert stored.title is None


def test_scan_skips_excluded_directories(tmp_path: Path) -> None:
    engine = _prepare_db(tmp_path)
    library_root = tmp_path / "library"
    included = library_root / "included"
    excluded = library_root / "excluded"
    included.mkdir(parents=True)
    excluded.mkdir(parents=True)

    _create_id3_file(included / "track.mp3")
    _create_id3_file(excluded / "skip.mp3")

    summary = scan_library(
        engine=engine,
        library_roots=[library_root],
        excluded_dirs=[excluded],
        batch_size=1,
        dry_run=False,
        atmos_detection_fn=lambda _: False,
    )

    assert summary.found_files == 1
    with Session(engine) as session:
        stored = session.scalars(select(File)).all()
        assert len(stored) == 1
        assert stored[0].path.endswith("included\\track.mp3")


def test_scan_persists_atmos_detection(tmp_path: Path) -> None:
    engine = _prepare_db(tmp_path)
    library_root = tmp_path / "library"
    library_root.mkdir()
    file_path = library_root / "track.mp3"
    _create_id3_file(file_path)

    summary = scan_library(
        engine=engine,
        library_roots=[library_root],
        dry_run=False,
        atmos_detection_fn=lambda path: path == file_path.resolve(),
    )

    assert summary.created == 1
    with Session(engine) as session:
        stored = session.execute(select(File)).scalar_one()
        assert stored.atmos_detected is True


def test_scan_builds_normalized_library_graph(tmp_path: Path) -> None:
    engine = _prepare_db(tmp_path)
    library_root = tmp_path / "library"
    artist_root = library_root / "Artist" / "Album"
    artist_root.mkdir(parents=True)

    _create_id3_file(artist_root / "track-1.mp3")
    second = artist_root / "track-2.mp3"
    _create_id3_file(second)
    tags = ID3(second)
    tags.delall("TIT2")
    tags.add(TIT2(encoding=3, text="Another Title"))
    tags.add(TRCK(encoding=3, text="2"))
    tags.save(second)

    summary = scan_library(
        engine=engine,
        library_roots=[library_root],
        dry_run=False,
        atmos_detection_fn=lambda _: False,
    )

    assert summary.created == 2
    with Session(engine) as session:
        artists = session.scalars(select(LibraryArtist)).all()
        albums = session.scalars(select(LibraryAlbum)).all()
        tracks = session.scalars(select(LibraryTrack).order_by(LibraryTrack.file_id.asc())).all()

        assert len(artists) == 1
        assert artists[0].name == "Test Artist"
        assert len(albums) == 1
        assert albums[0].title == "Test Album"
        assert len(tracks) == 2
        assert tracks[0].artist_id == artists[0].id
        assert tracks[0].album_id == albums[0].id
        assert tracks[0].normalized_artist_name == "test artist"
        assert tracks[0].normalized_album_title == "test album"
        assert tracks[1].normalized_title == "another title"
