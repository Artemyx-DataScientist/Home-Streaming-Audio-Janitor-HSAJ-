from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from hsaj.db.models import Base, File, RoonItemCache
from hsaj.roon import RoonTrack, cache_roon_track, match_track_by_metadata


def _in_memory_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_cache_roon_track_inserts_and_updates() -> None:
    with _in_memory_session() as session:
        track = RoonTrack(
            roon_track_id="roon-1",
            artist="Artist",
            album="Album",
            title="Title",
            duration_ms=180_000,
            track_number=3,
        )

        cache_roon_track(session, track)
        session.commit()

        cached = session.get(RoonItemCache, "roon-1")
        assert cached is not None
        assert cached.title == "Title"
        assert cached.track_number == 3

        updated = RoonTrack(
            roon_track_id="roon-1",
            artist="Artist",
            album="Album",
            title="Updated Title",
            duration_ms=181_000,
            track_number=3,
        )
        cache_roon_track(session, updated)
        session.commit()

        refreshed = session.get(RoonItemCache, "roon-1")
        assert refreshed is not None
        assert refreshed.title == "Updated Title"
        assert refreshed.duration_ms == 181_000


def test_match_track_returns_high_for_single_candidate() -> None:
    with _in_memory_session() as session:
        session.add(
            File(
                path="/music/track.flac",
                artist="Artist",
                album="Album",
                title="Title",
                track_number=5,
                duration_seconds=180,
            )
        )
        session.commit()

        track = RoonTrack(
            roon_track_id="roon-2",
            artist="artist",
            album="album",
            title="title",
            duration_ms=180_000,
            track_number=5,
        )

        result = match_track_by_metadata(session, track)

        assert result.confidence == "high"
        assert len(result.candidates) == 1
        assert result.candidates[0].path == "/music/track.flac"


def test_match_track_returns_low_when_no_candidates() -> None:
    with _in_memory_session() as session:
        session.add(
            File(
                path="/music/other.flac",
                artist="Someone",
                album="Else",
                title="Nothing",
                track_number=1,
                duration_seconds=120,
            )
        )
        session.commit()

        track = RoonTrack(
            roon_track_id="roon-3",
            artist="Missing",
            album="Album",
            title="Title",
            duration_ms=180_000,
            track_number=1,
        )

        result = match_track_by_metadata(session, track)

        assert result.confidence == "low"
        assert result.candidates == []


def test_match_track_returns_low_when_multiple_candidates() -> None:
    with _in_memory_session() as session:
        session.add_all(
            [
                File(
                    path="/music/one.flac",
                    artist="Artist",
                    album="Album",
                    title="Title",
                    track_number=2,
                    duration_seconds=200,
                ),
                File(
                    path="/music/two.flac",
                    artist="Artist",
                    album="Album",
                    title="Title",
                    track_number=2,
                    duration_seconds=201,
                ),
            ]
        )
        session.commit()

        track = RoonTrack(
            roon_track_id="roon-4",
            artist="Artist",
            album="Album",
            title="Title",
            duration_ms=200_500,
            track_number=2,
        )

        result = match_track_by_metadata(session, track, duration_tolerance_seconds=2)

        assert result.confidence == "low"
        assert len(result.candidates) == 2
