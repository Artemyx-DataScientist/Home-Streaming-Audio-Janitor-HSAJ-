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
python -m core.app
```

Bridge печатает сообщение о старте, а core запускает простой обработчик событий (заглушка), которые в дальнейшем будут заменены реальными компонентами.

## CI
GitHub Actions прогоняет линтеры и тесты для обеих частей проекта:
- `npm test` в `bridge` (включает ESLint);
- `ruff`, `black --check` и `pytest` в `core`.

## Структура
- `bridge/` — Node.js-мост к внешним системам.
- `core/` — Python-ядро с доменной логикой.
- `SPEC.md` — спецификация.
- `ARCHITECTURE.md` — обзор архитектуры.
- `adr/` — ADR-решения.

## Лицензия
MIT
