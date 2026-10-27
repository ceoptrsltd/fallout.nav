# Контракт: HTTP / WebSocket API fnav-core

Базовый адрес: `http://fnav.local/` (dev: `http://127.0.0.1:8000/`). Все JSON — UTF-8. Модели — `fnav/models.py`.

## Авторизация

- `POST /api/login` `{"password": "..."}` → `200 {"ok": true}` + cookie `fnav_session` (HttpOnly, SameSite=Lax, 30 дней). Неверно → `401`.
  Защита от перебора: после 5 неудачных попыток с IP — пауза 30 с.
- `POST /api/logout` → удаляет cookie. `GET /api/session` → `{"authenticated": bool}`.
- Пароль хранится как хэш `hashlib.scrypt` в `secrets.env` (`FNAV_PASSWORD_HASH=scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`), секрет подписи сессий — `FNAV_SESSION_SECRET`.
- **Без авторизации доступно:** `/api/login`, `/api/session`, `/admin/login.html` и статика логина;
  а с адреса `127.0.0.1` (киоск Chromium на самом устройстве) — также `/display/**`, `/ws`, `/tiles/**`, `/api/camera/mjpeg`, `/api/settings` (GET).
- Всё остальное без сессии → `401` (API) или редирект на `/admin/login.html` (страницы).
- Установленное приложение (PWA, `display-mode: standalone` / iOS Home Screen) при первом запуске на устройстве
  спрашивает пароль, даже если браузер уже вошёл (на Android приложение делит cookie с Chrome): метка
  `fnav.appLogin` в localStorage ставится только входом из самого приложения и снимается кнопкой Log out
  (`web/admin/js/app-login.js`). Это клиентская ступенька; защита — по-прежнему сессия на сервере.

## Статика

| Путь | Откуда |
|---|---|
| `/` | редирект на `/admin/` |
| `/admin/**` | `web/admin/` |
| `/display/**` | `web/display/` |

## WebSocket `/ws`

Сервер → клиент:
- `{"type":"telemetry", ...}` — модель `Telemetry`, **10 Гц**.
- `{"type":"settings","settings":{...}}` — сразу после подключения и после каждого изменения настроек.
- `{"type":"event","event":"button","value":"press"|"long_press"}`, `{"type":"event","event":"protected","segment":"..."}`, `{"type":"event","event":"toast","level":"info|warn|error","text":"..."}`.
- `{"type":"nmea","lines":[...]}` — только если клиент подписался.

Клиент → сервер:
- `{"type":"subscribe","topics":["nmea"]}` / `{"type":"unsubscribe","topics":["nmea"]}`.

## Настройки

- `GET /api/settings` → `Settings`. `GET /api/settings/defaults` → `Settings()` по умолчанию.
- `PATCH /api/settings` — частичное глубокое слияние `{"display":{"orientation":"north_up"}}` → валидированный полный `Settings`; `422` с описанием при ошибке.
- `PUT /api/settings` — полная замена. `POST /api/settings/reset` — сброс к умолчаниям.
- После сохранения: атомарная запись `settings.json`, рассылка по WS, `hub.apply_settings()`, `POST 127.0.0.1:8081/reload` (ошибку недоступности камеры игнорировать).

## Экран

- `POST /api/display/force` `{"mode":"auto"|"map"|"camera"}` → пишет `settings.display.force_mode`.
- Режим экрана и целевой зум вычисляет **fnav-core** (`fnav/core/navlogic.py`) — единый источник правды для физического экрана и превью; приходит в `Telemetry.display`.
  Логика: таблица `nav.zoom_table` (берётся ступень с наибольшим `min_mps` ≤ скорости), гистерезис `camera_below_mps`/`map_above_mps` с выдержкой `switch_delay_s`, смена ступени зума — после `zoom_delay_s`; нет фикса → `nav.no_fix_mode`; `force_mode` ≠ auto → `reason="forced"`.

## Тайлы

- `GET /tiles/{layer}/{z}/{x}/{y}` — `layer ∈ imagery|transport|places|street`. Порядок в URL Esri — `{z}/{y}/{x}` (см. `TileSettings`).
  Сначала дисковый кэш, иначе загрузка с Esri (таймаут 5 с, `token=` из `esri_api_key`, если задан), сохранение, ответ. Нет сети и нет в кэше → `404` (клиент рисует заглушку).
  Ответ с `Cache-Control: max-age=86400`. LRU-вытеснение по времени последнего доступа, лимит `storage.tile_cache_mb`.
- `GET /api/tiles/stats` → `{"size_mb":..,"count":..,"limit_mb":..,"hit_rate":..}`; `DELETE /api/tiles` — очистить.

## Записи

Файлы: `{recordings_dir}/YYYY-MM-DD/YYYY-MM-DD_HH-MM-SS.ts`, защищено = рядом `…ts.lock`, индекс ключевых кадров — `…ts.idx`. Подробно — `camera.md`.

- `GET /api/recordings` → `[{"path":"2026-10-20/2026-10-20_16-40-00.ts","date":"2026-10-20","start":"2026-10-20T16:40:00","size_bytes":..,"duration_s":..|null,"protected":bool,"in_progress":bool}]` (новые сверху).
- `GET /api/recordings/file/{path}` — отдача файла с поддержкой `Range` (`video/mp2t`).
- `GET /api/recordings/hls/{path}.m3u8` (`…/2026-10-20/2026-10-20_16-40-00.ts.m3u8`) — плейлист HLS по записи
  (`application/vnd.apple.mpegurl`, `Cache-Control: no-cache`) для перемотки в любое место без докачки:
  `EXT-X-VERSION:4`, `PLAYLIST-TYPE:EVENT`, `EXT-X-START:TIME-OFFSET=0`; куски ~3 с (≤ 3 МБ) —
  `EXT-X-BYTERANGE` того же `.ts` по относительному адресу `../../file/{path}` (работает и под `/fnadmin/`),
  каждый начинается с PAT/PMT и ключевого кадра, `EXTINF` — точно по PTS. Запись закончена — `EXT-X-ENDLIST`;
  идёт запись — без него, только законченные куски (плеер перечитывает, старые куски не меняются).
  Ключевые кадры — из `.ts.idx` регистратора или поиском по файлу (camera.md). Не MPEG-TS — плейлист без кусков.
  Админка: Safari и iPhone — `video.src = m3u8` (свой HLS), остальные — hls.js (`web/admin/vendor/`).
- `POST /api/recordings/protect` `{"path":"...","protected":true}`; `POST /api/recordings/protect-current`.
- `DELETE /api/recordings/file/{path}` — запрещено для `in_progress` (`409`) и защищённых без `?force=1`;
  вместе с записью удаляются `.lock` и индекс `.ts.idx`.
- Защита от выхода за каталог (`..`, абсолютные пути) обязательна.

## Камера (прокси к fnav-camera `127.0.0.1:8081`)

- `GET /api/camera/status` → `CameraStatus`.
- `GET /api/camera/mjpeg` → поток `multipart/x-mixed-replace` 768×576 (для экрана и превью).
- `GET /api/camera/snapshot.jpg`.
- `POST /api/camera/whep` — прокси WebRTC WHEP к MediaMTX `http://127.0.0.1:8889/cam/whep` (тело SDP и заголовки `Content-Type`/`Location` проброшены; `PATCH`/`DELETE` по `Location` — тоже через `/api/camera/whep/...`).
- Кнопка на GPS-модуле: `press` (короткое) → вкл/выкл видеовыход на монитор (`POST /api/display/output`, wlr-randr у cage, ЦАП обесточивается); `long_press` (2 с) → `POST 127.0.0.1:8081/protect-current` + событие WS + сигнал.
- LED на модуле: `recording` → `blink_slow` (мигает), `error` → `blink_fast`, нет записи → `off`; после нажатия кнопки 3 с подтверждение: `on` (защищено) или `blink_fast` (ошибка).

## Датчики

- `GET /api/gps/satellites` → `[Satellite]`. `POST /api/gps/configure` → результат `hub.gps_configure()`.
- `POST /api/compass/calibration/start|finish|cancel`, `GET /api/compass/calibration` → `CalibrationStatus`.
  `finish` сохраняет результат в `settings.compass.calibration` и возвращает его.
- **Автокалибровка по GPS-курсу** (на ходу, `settings.compass.auto_calibrate`): хаб сам собирает точки и вызывает `on_calibration_update(cal)`; fnav-core сохраняет `cal` в `settings.compass.calibration`. Состояние — `Telemetry.compass_cal` (`AutoCalStatus`).
  `POST /api/compass/calibration/reset-auto` — сбросить накопленные точки (сохранённую калибровку не трогает).

## Охрана и уровень (MPU-6050)

Полностью — в отдельных контрактах, здесь только перечень:

- **Охрана** — [alarm.md](alarm.md) §4: `GET /api/alarm`, `POST /api/alarm/arm|disarm|silence|test|hazard/test`,
  `GET /api/alarm/snapshot/{id}.jpg`, `DELETE /api/alarm/events`, `POST /api/alarm/simulate` (только `--mock`, иначе 404). Настройки — `settings.alarm`,
  телеметрия — `Telemetry.alarm`, клавиша — действие `"alarm"`. Камера на охране (fnav-watch) — [vision.md](vision.md).
- **Уровень** — [level.md](level.md): `GET /api/level`, `POST /api/level/calibration/reference|forward`,
  `DELETE /api/level/calibration`. Настройки и калибровка — `settings.level`, телеметрия — `Telemetry.level`.

## Система

- `GET /api/system/status` → `{"system": SystemStatus, "disk": DiskStatus, "services": {"fnav-camera":"active",...}, "version":"..."}`.
- `GET /api/system/logs?unit=fnav-core&lines=200` → `{"lines":[...]}` (`journalctl`, только `fnav-*`, `mediamtx`, `gpsd`).
- `POST /api/system/reboot`, `POST /api/system/shutdown`.
- `GET /api/system/wifi` → `[{"ssid","priority","active","signal_pct"}]`; `POST /api/system/wifi` `{"ssid","password","priority"}`; `DELETE /api/system/wifi/{ssid}` (через `nmcli`).
- `POST /api/system/password` `{"old","new"}` (новый ≥ 6 символов).
- В mock-режиме системные действия не выполняются, возвращают `{"ok":true,"mock":true}`.

## Ошибки

`{"detail": "message in English"}` + код (`400/401/404/409/422/429/500/503`; 429 + `Retry-After` — блокировка после 5 неудачных входов).
