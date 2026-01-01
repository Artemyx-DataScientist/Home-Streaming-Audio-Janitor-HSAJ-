from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypedDict


class EventPayload(TypedDict, total=False):
    """Допустимые поля полезной нагрузки события."""

    track_id: str
    user_id: str
    quality: str


@dataclass
class StreamingEvent:
    """Нормализованное событие, поступающее из bridge."""

    source: str
    first_seen: datetime
    payload: EventPayload
    atmos: bool = False

    def describe(self) -> str:
        """Формирует строку с ключевыми атрибутами события."""

        atmos_flag = "with Atmos" if self.atmos else "without Atmos"
        return (
            f"[{self.source}] first_seen={self.first_seen.isoformat()} "
            f"quality={self.payload.get('quality', 'unknown')} {atmos_flag}"
        )


def main() -> None:
    """Точка входа dev-скелета core."""

    example_event = StreamingEvent(
        source="dev-bridge",
        first_seen=datetime.now(tz=timezone.utc),
        payload={"quality": "lossless"},
        atmos=True,
    )
    print("HSAJ core ready. Example event:")
    print(example_event.describe())


if __name__ == "__main__":
    main()
