# Home Streaming Audio Janitor (HSAJ)

Базовый скелет проекта для мостика (bridge) и ядра (core). Репозиторий подготовлен для дальнейшей разработки и интеграции.

## Как запустить bridge + core в dev

### Предварительные требования
- Node.js 18+
- Python 3.11+
- `npm`, `python`, `pip`

### Установка зависимостей
```bash
cd bridge
npm install
cd ../core
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

### Запуск в разработке
В одном терминале запускаем bridge:
```bash
cd bridge
npm run dev
```

В другом терминале запускаем core:
```bash
cd core
source .venv/bin/activate
python -m core.app  # или hsaj listen --config configs/hsaj.yaml
```

Bridge печатает сообщение о старте, поднимает WebSocket-канал `/events` и рассылает события
`transport_event` при смене трека. Core подключается по `HSAJ_BRIDGE_WS` (по умолчанию
`ws://localhost:8080/events`), логирует и записывает историю воспроизведений в SQLite.

## CI
GitHub Actions прогоняет линтеры и тесты для обеих частей проекта:
- `npm test` в `bridge` (включает ESLint);
- `ruff`, `black --check` и `pytest` в `core`.

## Запуск под systemd
Готовые юниты и инструкция по включению сервисов/таймера лежат в `configs/systemd` и описаны в
[`docs/systemd.md`](docs/systemd.md).

## Структура
- `bridge/` — Node.js-мост к внешним системам.
- `core/` — Python-ядро с доменной логикой.
- `SPEC.md` — спецификация.
- `ARCHITECTURE.md` — обзор архитектуры.
- `adr/` — ADR-решения.

## Лицензия
MIT
