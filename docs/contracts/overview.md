# Контракты fallout.nav — обзор (читать всем агентам первым)

Общий план: `PLAN.md` (разделы 1–11). Здесь — то, о чём части договорились между собой.

## Источники правды (владелец — ведущий, агенты НЕ правят)

| Файл | Что |
|---|---|
| `fnav/models.py` | все модели: `Settings`, `SensorSnapshot`, `CameraStatus`, `Telemetry` |
| `fnav/interfaces.py` | интерфейс `SensorHub` + фабрика `create_sensor_hub(settings, mock)` |
| `fnav/paths.py` | пути к данным (`FNAV_DATA_DIR`, по умолчанию `./.devdata`) |
| `fnav/mock/sensors.py` | `MockSensorHub` — синтетический маршрут для разработки |
| `docs/contracts/*.md` | REST/WS API, экран, камера, система |

Нужно изменить контракт → **не меняйте сами**, опишите в финальном отчёте («нужно поле X в модели Y, потому что …»). Временно можно обойтись локально и пометить `# TODO(contract)`.

## Зоны (кто какие файлы пишет)

| Агент | Зона | Не трогает |
|---|---|---|
| A · backend | `fnav/core/**`, `tests/core/**` | всё остальное |
| B · рендер карты | `web/display/index.html`, `web/display/app.js`, `web/display/map/**`, `tests/display/**` (если нужны) | `web/display/ui/**` |
| C · дизайн | `web/display/ui/**` | остальное в `web/display/` |
| D · веб-интерфейс | `web/admin/**` | — |
| E · камера/регистратор | `fnav/camera/**`, `tests/camera/**` | — |
| F · датчики | `fnav/sensors/**`, `tools/ubx_config.py`, `tools/i2c_scan.py`, `tests/sensors/**` | — |
| G · система Pi | `pi/**` | — |

**Git:** ведущий коммитит сам. Агенты **не делают `git commit`/`git add`/`git stash`/`checkout`** — только пишут файлы своей зоны.

## Процессы и порты (на устройстве)

| Сервис systemd | Команда | Слушает |
|---|---|---|
| `fnav-core` | `/opt/fnav/venv/bin/python -m fnav.core` | `0.0.0.0:80` (веб, API, WS, тайлы) |
| `fnav-camera` | `/opt/fnav/venv/bin/python -m fnav.camera` | `127.0.0.1:8081` (статус, MJPEG, управление) |
| `mediamtx` | `/usr/local/bin/mediamtx /etc/fnav/mediamtx.yml` | RTSP `127.0.0.1:8554`, WebRTC HTTP `127.0.0.1:8889`, WebRTC ICE UDP `:8189` |
| `fnav-display` | `cage -- chromium --kiosk http://127.0.0.1/display/?pal=1` | — (вывод на HDMI 720×576) |
| `gpsd` | системный, устройство `/dev/ttyAMA0` | `127.0.0.1:2947` |
| `chrony` | системный, refclock от gpsd (SHM) | — |

- Код: `/opt/fnav/app` (копия репозитория), venv: `/opt/fnav/venv` **с `--system-site-packages`** (picamera2, libcamera, python3-gps, lgpio ставятся из apt).
- Данные: `/var/lib/fnav` (`settings.json`, `recordings/`, `tiles/`), секреты: `/etc/fnav/secrets.env`.
- Пользователь сервисов: `fnav` (группы `video`, `i2c`, `gpio`, `dialout`, `systemd-journal`, `netdev`).
- ОС: Raspberry Pi OS Lite 64-bit на Debian 13 (trixie), Python 3.13. Код должен работать и на Python 3.11 (ПК разработчика, Windows).

## Режим разработки на ПК

```
python -m fnav.core --mock --port 8000          # backend + mock-датчики + mock-камера
# открыть http://127.0.0.1:8000/admin/  и  http://127.0.0.1:8000/display/
```

Mock-камера (тестовая картинка MJPEG) — часть `fnav.core` в режиме `--mock`, если `127.0.0.1:8081` не отвечает.
Пароль в dev-режиме: если `secrets.env` отсутствует, `fnav.core --mock` создаёт его с паролем `fnav-dev`.
**Настоящий пароль устройства в репозиторий не попадает** — его задаёт ведущий при установке.

## Общие правила

- **All user-facing text is ENGLISH only** (web UI, display HUD, API error messages, statuses, logs shown to the user). No Russian in the interface. Code comments — English or Russian, brief.
- Никаких внешних CDN в рантайме: в машине интернета может не быть. Все JS/CSS/шрифты — локально в `web/`.
- Фронтенд — **без сборки**: ES-модули, `<script type="module">`.
- Python: типизация, `asyncio`, без блокирующих вызовов в event loop (блокирующее — в `asyncio.to_thread`).
- Всё должно переживать отсутствие устройства/сети: ошибка → понятное сообщение в статусе, повторные попытки, без падения процесса.
