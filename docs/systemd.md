# Запуск HSAJ под systemd

Эти юниты позволяют поднять bridge как сервис, а core — запускать по таймеру в режиме dry-run.
Все файлы лежат в `configs/systemd` и предполагают настройку через `/etc/hsaj/hsaj.env`.

## Какие юниты доступны
- `hsaj-bridge.service` — поднимает bridge (`node src/index.js`). Запускается при старте системы.
- `hsaj-core.service` — oneshot-сервис, который последовательно выполняет `hsaj scan`, `hsaj roon sync`, `hsaj apply --dry-run`.
- `hsaj-core.timer` — ежедневный запуск `hsaj-core.service` в 03:00 c джиттером до 15 минут. Триггер после пропуска перезагрузки сохраняется (`Persistent=true`).

## Подготовка окружения
1. Убедитесь, что Node.js 18+ и Python 3.11+ установлены и доступны в PATH.
2. Разверните HSAJ в директорию (по умолчанию используется `/opt/hsaj`).
3. Скопируйте и настройте конфиг ядра, например:
   ```bash
   sudo install -d /etc/hsaj
   sudo cp configs/hsaj.example.yaml /etc/hsaj/hsaj.yaml
   sudo chown root:root /etc/hsaj/hsaj.yaml
   ```
4. Подготовьте окружение systemd:
   ```bash
   sudo cp configs/systemd/hsaj.env.example /etc/hsaj/hsaj.env
   sudo chmod 640 /etc/hsaj/hsaj.env
   ```
   В файле `/etc/hsaj/hsaj.env` пропишите:
   - `HSAJ_ROOT` — путь до корня репозитория или установленной сборки;
   - `HSAJ_CONFIG` — путь до `hsaj.yaml` (если не задан, cli ищет `configs/hsaj.yaml` относительно `HSAJ_ROOT`);
   - `PATH` — добавьте `bin` вашего виртуального окружения Python, чтобы `hsaj` был доступен;
   - `BRIDGE_PORT`, `BRIDGE_WS_PATH`, `HSAJ_BRIDGE_WS` — если порт/путь отличаются от дефолтов;
   - `BRIDGE_DISABLE_DEMO=1`, чтобы выключить демо-генератор событий.

## Установка и включение юнитов
```bash
sudo install -o root -g root -m 644 configs/systemd/hsaj-bridge.service /etc/systemd/system/
sudo install -o root -g root -m 644 configs/systemd/hsaj-core.service /etc/systemd/system/
sudo install -o root -g root -m 644 configs/systemd/hsaj-core.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hsaj-bridge.service
sudo systemctl enable --now hsaj-core.timer
```

Проверить состояние:
```bash
sudo systemctl status hsaj-bridge.service
sudo systemctl status hsaj-core.service
sudo systemctl list-timers hsaj-core.timer
```

## Тонкая настройка
- Чтобы изменить расписание, отредактируйте `OnCalendar` в drop-in файле:
  ```bash
  sudo systemctl edit hsaj-core.timer
  ```
  и перезагрузите таймер: `sudo systemctl daemon-reload && sudo systemctl restart hsaj-core.timer`.
- Сервис `hsaj-core.service` запускается в режиме `--dry-run` по умолчанию. Уберите флаг в drop-in или запустите вручную без dry-run:
  ```bash
  sudo systemctl start hsaj-core.service
  sudo systemctl status hsaj-core.service
  # Или в drop-in: ExecStart=/usr/bin/env bash -c 'cd "${HSAJ_ROOT}/core" && exec hsaj apply'
  ```
- Если нужно запускать под отдельным пользователем, добавьте `User=` и `Group=` в drop-in для соответствующих юнитов.
