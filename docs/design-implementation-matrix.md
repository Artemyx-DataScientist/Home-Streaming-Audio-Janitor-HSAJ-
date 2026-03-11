# Сопоставление `DESIGN.md` и текущей реализации HSAJ

## Источники и обозначения

- Источник ожиданий: `DESIGN.md`.
- Источник факта: runtime-код в `bridge/` и `core/`.
- Тесты используются как вторичное подтверждение поведения, но не подменяют runtime-реализацию.
- Метки в доказательствах:
  - `[runtime]` — поведение видно в исполняемом коде.
  - `[test]` — поведение подтверждено тестом.
  - `[README]` — поведение заявлено в документации, но не обязательно обеспечено runtime-кодом.

## 1. Сводная матрица

| Раздел дизайна | Ожидание | Факт в коде | Статус | Доказательства | Комментарий/разрыв |
| --- | --- | --- | --- | --- | --- |
| `0. Краткая идея` | Система следит за библиотекой, получает сигналы от Roon, выделяет Atmos, готовит удаление, а истина живёт в FS + своей БД. | Базовый контур есть: `scan` пишет метаданные в SQLite, bridge шлёт transport events, planner строит план Atmos/quarantine, executor перемещает файлы и пишет audit log. | `Частично реализовано` | `DESIGN.md:5-15`; [runtime] `core/src/hsaj/scanner.py:184-216`; [runtime] `core/src/hsaj/db/models.py:37-179`; [runtime] `core/src/hsaj/planner.py:216-271`; [runtime] `core/src/hsaj/executor.py:151-190`; [runtime] `bridge/src/index.js:255-410` | Общая идея MVP реализована, но “находит мусор и дубли”, полноценные blocked-сигналы и пользовательские сигналы Roon пока не доведены до end-to-end состояния. |
| `1. Цели и не-цели` | Автоматизировать уборку, защищать Atmos и favorites/whitelist, уважать блоки Roon, делать действия обратимыми, не писать свой плеер и не лезть в БД Roon. | Система не является плеером, работает через bridge и transport API Roon, Atmos переносится отдельно, quarantine/restore обратимы. Favorites/whitelist и inbox-автораскладка отсутствуют. | `Частично реализовано` | `DESIGN.md:23-42`; [runtime] `bridge/src/index.js:356-387`; [runtime] `core/src/hsaj/atmos.py:131-205`; [runtime] `core/src/hsaj/executor.py:106-148`; [runtime] `core/src/hsaj/executor.py:211-298`; [test] `core/tests/test_planner_executor.py:123-247` | Не-цели соблюдены лучше, чем цели: reversible quarantine уже есть, а favorites/whitelist и “заливка в /inbox” пока отсутствуют. |
| `2. Архитектура компонентов` | File Scanner + Roon Integration Layer + Core + Action Executor + CLI/Web UI. | Компоненты есть как отдельные модули и процессы: `bridge/src/index.js`, `scanner.py`, `blocking.py`, `planner.py`, `executor.py`, `transport.py`, `cli.py`. Веб-интерфейса и core HTTP API нет. | `Частично реализовано` | `DESIGN.md:48-95`; [runtime] `bridge/src/index.js:255-410`; [runtime] `core/src/hsaj/scanner.py:184-216`; [runtime] `core/src/hsaj/blocking.py:163-244`; [runtime] `core/src/hsaj/planner.py:216-271`; [runtime] `core/src/hsaj/executor.py:151-190`; [runtime] `core/src/hsaj/cli.py:33-351` | Архитектурное разделение уже похоже на драфт, но scanner и bridge проще задуманного, а “веб-интерфейс/FastAPI” пока не начинался. |
| `3. Источники данных и истина` | FS — истина о файлах, Roon — истина о пользовательских решениях и поведении, локальная БД хранит `files`, `library_items`, `roon_blocks`, `block_candidates`, `play_history`, `actions_log`. | `files`, `play_history`, `actions_log`, `roon_blocks_raw`, `block_candidates`, `roon_items_cache` есть. `library_items`/`tracks` нет. FS-метаданные ограничены путём, размером, форматом, тегами и duration; каналов, bitrate и Atmos-флага в БД нет. | `Частично реализовано` | `DESIGN.md:101-135`; [runtime] `core/src/hsaj/db/models.py:37-179`; [runtime] `core/src/hsaj/scanner.py:80-110`; [runtime] `core/src/hsaj/transport.py:123-171` | Источник истины уже смещён в FS + SQLite, как и задумывалось, но модель данных пока упрощённая и не покрывает логические сущности библиотеки. |
| `4. Логика блоков и удаления` | Наследование `artist -> album -> track`, таймер от `first_seen_at`, restore при снятии блока, pre-check перед действием, quarantine и опциональное auto-delete. | Таймер от `first_seen_at` и restore при повторном sync реализованы. Но planner реально умеет строить quarantine-план только для `track`, когда есть `RoonItemCache`; `artist`/`album` не разворачиваются в набор файлов. Pre-check ограничен проверкой факта существования файла и Atmos-каталога; whitelist и второй таймер удаления отсутствуют. | `Частично реализовано` | `DESIGN.md:141-187`; [runtime] `core/src/hsaj/blocking.py:70-205`; [test] `core/tests/test_blocking.py:18-120`; [runtime] `core/src/hsaj/planner.py:88-213`; [runtime] `core/src/hsaj/executor.py:106-190`; [test] `core/tests/test_planner_executor.py:89-200` | Самая большая функциональная дыра: дизайн описывает иерархическое наследование блоков, а текущий код работает как “прямые blocked objects + попытка сопоставить один track по метаданным”. |
| `5. Работа с Dolby Atmos` | `ffprobe`-детекция по profile/tags, перенос в отдельный каталог, иммунитет от автоудаления. | Детекция через `ffprobe` по `stream.profile`, `stream.tags`, `format.tags` реализована. Planner строит `atmos_moves`, executor перемещает файл в `atmos_dir`, а quarantine-план не строится для файлов, уже лежащих внутри Atmos-каталога. | `Частично реализовано` | `DESIGN.md:193-213`; [runtime] `core/src/hsaj/atmos.py:21-96`; [runtime] `core/src/hsaj/atmos.py:117-205`; [runtime] `core/src/hsaj/planner.py:174-190`; [runtime] `core/src/hsaj/executor.py:72-103` | Политика реализована близко к драфту, но immunity проверяется по расположению в `atmos_dir`, а не по отдельному сохранённому Atmos-флагу в БД. |
| `6. Поведенческий скоринг` | Soft-scoring по never played / age / likes / inbox age / duplicates, вывод в `soft_candidates`, без автоделита. | Отдельного behavior scoring нет. Поле `low_confidence` в плане — это не soft-scoring, а список кандидатов, которые не удалось уверенно сопоставить с файлом. | `Не реализовано` | `DESIGN.md:217-233`; [runtime] `core/src/hsaj/planner.py:31-46`; [runtime] `core/src/hsaj/planner.py:137-167`; [runtime] `core/src/hsaj/transport.py:123-171` | `play_history` уже собирается, но не используется для поведенческой аналитики, never-played логики, duplicate detection или ручного soft quarantine. |
| `7. CLI / API` | `hsaj scan`, `hsaj sync-roon`, `hsaj plan`, `hsaj apply`, `hsaj history` и будущий web API: `/plan`, `/apply`, `/stats`, `/candidates`. | CLI уже даёт `scan`, `plan`, `apply`, `listen`, `restore`, `db init/status`, `roon sync`. Команда названа `hsaj roon sync`, а не `hsaj sync-roon`. `history` отсутствует. Core web API нет. Bridge предоставляет только `/health`, `/track/{id}`, `/blocked`, WS `/events`. | `Реализовано иначе` | `DESIGN.md:239-269`; [runtime] `core/src/hsaj/cli.py:95-351`; [runtime] `bridge/src/index.js:255-329`; [README] `README.md:41-45` | CLI в целом уже полезный, но интерфейс сдвинут в сторону namespace-команд, а web API реализован только как bridge API, не как HTTP-слой поверх core. |
| `8. Технологический стек` | Python + FastAPI + SQLite, Node.js bridge, HTTP/WebSocket связь, `ffprobe`, `mutagen`. | Python 3.11+, SQLite/SQLAlchemy, Node.js bridge, WebSocket, `mutagen` и `ffprobe` действительно используются. FastAPI нет; CLI построен на Typer, bridge общается через Node HTTP server и WS. | `Реализовано иначе` | `DESIGN.md:275-284`; [runtime] `core/pyproject.toml:6-17`; [runtime] `bridge/package.json:6-19`; [runtime] `bridge/src/index.js:1-15`; [runtime] `core/src/hsaj/atmos.py:21-96`; [runtime] `core/src/hsaj/scanner.py:58-110` | Стек совпадает по базовым технологиям, но API-слой ушёл не в FastAPI, а в комбинацию Typer + минимальный Node HTTP/WS bridge. |
| `9. Конфигурация` | `hsaj.toml`/`config.yaml` с путями, grace days, quarantine delete days, `auto_delete`, `enable_behavior_scoring`. | Реализован YAML-конфиг с `database` и `paths`: `library_roots`, `quarantine_dir`, `atmos_dir`, `inbox_dir`, scan options, `ffprobe_path`. Политические параметры (`block_grace_days`, `quarantine_delete_days`, `auto_delete`, `enable_behavior_scoring`) в конфиг не вынесены. Grace period пока задаётся CLI-флагом `--grace-days`. | `Реализовано иначе` | `DESIGN.md:288-303`; [runtime] `core/src/hsaj/config.py:21-178`; [runtime] `configs/hsaj.example.yaml:1-19`; [runtime] `core/src/hsaj/cli.py:293-315` | Конфиг уже рабочий, но он покрывает файловую инфраструктуру, а не policy-слой из дизайн-драфта. |
| `10. Структура репозитория` | Примерная структура с `configs/`, `docs/decisions/ADR-*`, разложенным bridge на несколько модулей, `test/`, `scripts/`. | Верхнеуровневое разделение `bridge/`, `core/`, `configs/`, `docs/`, `adr/` есть, но bridge пока монолитен в `bridge/src/index.js`, ADR лежат в `adr/`, а не в `docs/decisions/`. | `Частично реализовано` | `DESIGN.md:306-340`; [runtime] фактические каталоги `bridge/`, `core/`, `configs/`, `docs/`, `adr/`; [runtime] `bridge/src/index.js`; [runtime] `README.md:3-5` | Структура движется в ту же сторону, но код ещё не разложен на более мелкие модули, которые предполагает драфт. |

## Сквозные интерфейсы

- CLI:
  - Есть `hsaj scan`, `hsaj plan`, `hsaj apply`, `hsaj listen`, `hsaj restore`, `hsaj roon sync`, `hsaj db init`, `hsaj db status`.
  - Нет `hsaj history`.
  - Доказательства: [runtime] `core/src/hsaj/cli.py:95-351`.
- Bridge HTTP/WebSocket API:
  - Есть `GET /health`, `GET /track/{id}`, `GET /blocked`, WebSocket `/events`.
  - `/blocked` сейчас технически присутствует, но по умолчанию возвращает `501`, потому что provider передаётся как `() => null`.
  - Доказательства: [runtime] `bridge/src/index.js:255-329`; [runtime] `bridge/src/index.js:391-405`.
- SQLite-модели:
  - Есть `files`, `actions_log`, `play_history`, `roon_items_cache`, `roon_blocks_raw`, `block_candidates`.
  - Нет `library_items` и отдельной логической модели album/artist/track.
  - Доказательства: [runtime] `core/src/hsaj/db/models.py:37-179`.
- Файловые операции:
  - Есть scan/upsert, Atmos move, quarantine move, restore.
  - Нет физического auto-delete после второго таймера.
  - Доказательства: [runtime] `core/src/hsaj/scanner.py:184-268`; [runtime] `core/src/hsaj/atmos.py:131-205`; [runtime] `core/src/hsaj/executor.py:72-190`; [runtime] `core/src/hsaj/executor.py:211-298`.

## 2. Детальный разбор ключевых расхождений

### 2.1. Иерархия блокировок `artist -> album -> track`

Дизайн предполагает каскадное наследование блоков и приоритет `track > album > artist` (`DESIGN.md:141-157`). Текущий код хранит `object_type` и `object_id` для каждого blocked object, но не разворачивает `artist` и `album` в набор файлов.

- `sync_blocked_objects()` просто создаёт или обновляет `BlockCandidate` с той же парой `object_type/object_id`, не вычисляя дочерние сущности: [runtime] `core/src/hsaj/blocking.py:163-205`.
- `_load_cached_track()` в planner работает только для `candidate.object_type == "track"`: [runtime] `core/src/hsaj/planner.py:88-103`.
- Если cached track отсутствует или сопоставление неоднозначно, кандидат попадает в `low_confidence`, а не в quarantine-план: [runtime] `core/src/hsaj/planner.py:137-167`.

Практический эффект: дизайн обещает, что блокировка артиста/альбома приведёт к конкретным кандидатам на вынос, а текущая реализация по runtime-коду такого пути не содержит.

### 2.2. Таймеры, pre-check и restore-логика

В части `first_seen_at` текущая реализация уже хорошо совпадает с драфтом.

- `upsert_raw_block()` и `upsert_block_candidate()` сохраняют `first_seen_at`, обновляют `last_seen_at` и не сдвигают `planned_action_at` при повторном sync: [runtime] `core/src/hsaj/blocking.py:70-136`; [test] `core/tests/test_blocking.py:63-90`.
- При исчезновении blocked object кандидат переводится в `restored`, а `planned_action_at` очищается: [runtime] `core/src/hsaj/blocking.py:139-160`; [test] `core/tests/test_blocking.py:93-120`.
- После quarantine есть явный restore flow с логированием конфликта и возвратом файла назад: [runtime] `core/src/hsaj/executor.py:201-298`; [test] `core/tests/test_planner_executor.py:203-247`.

Но политика pre-check из дизайна реализована лишь частично:

- нет whitelist/favorites-проверки;
- нет повторной валидации blocked state в момент `apply`;
- нет второго таймера `delete_after` и физического удаления старых quarantine-файлов.

Иными словами, reversible-часть уже существует, а hard-delete policy пока нет.

### 2.3. Soft-scoring и “вторая линия обороны”

Раздел `DESIGN.md:217-233` пока не реализован.

- `play_history` действительно собирается из transport events: [runtime] `core/src/hsaj/transport.py:123-171`; [test] `core/tests/test_transport.py:47-115`.
- Но planner не использует `play_history`, возраст файлов, likes/tags, inbox age или качество дублей для формирования `soft_candidates`.
- Текущее поле `low_confidence` не равно soft-scoring: это технический список кандидатов, которые не удалось связать с одним файлом по metadata match: [runtime] `core/src/hsaj/planner.py:31-46`; [runtime] `core/src/hsaj/planner.py:153-167`.

Практический эффект: сегодня система умеет собирать события прослушивания, но ещё не использует их для product-level решений об уборке библиотеки.

### 2.4. Web API “на будущее”

В design draft web API относится к ядру HSAJ: `GET /plan`, `POST /apply`, `GET /stats`, `GET /candidates` (`DESIGN.md:262-269`). Сейчас этого слоя нет.

- Core предоставляет только CLI-команды: [runtime] `core/src/hsaj/cli.py:128-351`.
- HTTP/WS API есть только у bridge и заточен под transport-события и наблюдаемые треки: [runtime] `bridge/src/index.js:255-329`.

Это не просто “не всё доделано”, а именно другая граница ответственности: bridge уже наружу торчит, core пока остаётся локальным CLI/worker.

### 2.5. Полнота Roon blocked integration

Именно этот участок сейчас наиболее слабый end-to-end.

- Bridge объявляет `GET /blocked`, но при текущей wiring-конфигурации отдаёт `501 Not Implemented`: [runtime] `bridge/src/index.js:269-275`; [runtime] `bridge/src/index.js:396-402`; [README] `README.md:41-45`.
- `hsaj roon sync` жёстко зависит от `/blocked`: [runtime] `core/src/hsaj/cli.py:293-344`; [runtime] `core/src/hsaj/blocking.py:208-244`.
- Bridge умеет отдавать `/track/{id}`, но трековые идентификаторы там синтезируются из zone/title/artist/album/duration, а не берутся из явного Roon blocked API: [runtime] `bridge/src/index.js:79-83`; [runtime] `bridge/src/index.js:97-140`; [runtime] `bridge/src/index.js:243-245`.

Практический эффект: transport/history flow уже рабочий, а blocked sync остаётся частично каркасным и пока не выглядит полноценно замкнутым на реальный источник blocked objects.

## 3. Вывод по зрелости реализации

### Что уже образует рабочий MVP

- Разделение на bridge и core уже материализовано.
- Библиотечный scan с записью в SQLite работает.
- Transport events из Roon bridge попадают в `play_history`.
- Atmos detection и перенос в отдельный каталог реализованы.
- Планирование quarantine и фактическое перемещение файлов реализованы.
- Restore из quarantine и audit logging уже есть.

### Что пока остаётся концептом из дизайн-драфта

- Наследование блокировок от artist/album к конкретным файлам.
- Рабочий источник blocked/banned/hidden данных из bridge.
- Favorites/whitelist.
- Поведенческий soft-scoring и duplicate quality logic.
- Core HTTP API / веб-слой.
- Второй таймер и auto-delete из quarantine.
- Логическая модель `library_items`/`tracks`.

### Самые важные расхождения для пользовательского поведения

1. Пользовательский blocked flow из дизайна не замкнут end-to-end: bridge не поставляет реальные blocked objects по `/blocked`, а core без этого не может синхронизировать блокировки.
2. Даже если blocked objects появятся, planner по runtime-коду уверенно обрабатывает только `track`-кандидатов, а не иерархию `artist/album`.
3. Система уже хорошо справляется с Atmos/quarantine/history, но “интеллект уборки” из design draft пока почти полностью отсутствует.

### Общая оценка

Текущая кодовая база больше всего похожа на ранний, но уже полезный MVP вокруг четырёх потоков: `scan`, `listen transport`, `plan/apply quarantine`, `Atmos move`. Она следует архитектурному направлению `DESIGN.md`, но пока реализует только “жёсткое ядро” санитарного конвейера, а не весь продуктовый объём, описанный в драфте.
