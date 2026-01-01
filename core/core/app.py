from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from hsaj.config import ConfigError, LoadedConfig, find_config_path, load_config
from hsaj.db import init_database
from hsaj.transport import DEFAULT_BRIDGE_WS_URL, TransportEventProcessor, listen_to_bridge


def _load_config() -> LoadedConfig:
    config_path_env = os.environ.get("HSAJ_CONFIG")
    config_path = Path(config_path_env) if config_path_env else None
    resolved_path = find_config_path(config_path)
    return load_config(resolved_path)


def _build_session_factory(engine: Engine) -> Callable[[], Session]:
    def _factory() -> Session:
        return Session(engine)

    return _factory


def main() -> None:
    """Точка входа: подключение к bridge по WebSocket и логирование событий."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ws_url = os.environ.get("HSAJ_BRIDGE_WS", DEFAULT_BRIDGE_WS_URL)

    try:
        loaded_config = _load_config()
    except ConfigError as exc:
        logging.error("Не удалось загрузить конфиг: %s", exc)
        raise SystemExit(1) from exc

    engine, _ = init_database(loaded_config.config.database)
    processor = TransportEventProcessor(session_factory=_build_session_factory(engine))
    stop_event = asyncio.Event()

    try:
        asyncio.run(listen_to_bridge(ws_url=ws_url, processor=processor, stop_event=stop_event))
    except KeyboardInterrupt:
        stop_event.set()
        logging.info("Отключение от bridge по Ctrl+C")


if __name__ == "__main__":
    main()
