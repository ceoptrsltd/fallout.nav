# Контракт: HTTP / WebSocket API fnav-core

Базовый адрес: `http://fnav.local/` (dev: `http://127.0.0.1:8000/`). Все JSON — UTF-8. Модели — `fnav/models.py`.

## Авторизация

- `POST /api/login` `{"password": "..."}` → `200 {"ok": true}` + cookie `fnav_session` (HttpOnly, SameSite=Lax, 30 дней). Неверно → `401`.
  Защита от перебора: после 5 неудачных попыток с IP — пауза 30 с.
- `POST /api/logout` → удаляет cookie. `GET /api/session` → `{"authenticated": bool, "local", "remote", "mock"}`;
  со входом ещё `"timezone": "Europe/Warsaw"|null` — пояс устройства (см. «Часовой пояс»; без входа не отдаётся:
  пояс ≈ страна, где машина).
- Пароль хранится как хэш `hashlib.scrypt` в `secrets.env` (`FNAV_PASSWORD_HASH=scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`), секрет подписи сессий — `FNAV_SESSION_SECRET`.
- **Без авторизации доступно:** `/api/login`, `/api/session`, `/admin/login.html` и статика логина;
  а с адреса `127.0.0.1` (киоск Chromium на самом устройстве) — также `/display/**`, `/ws`, `/tiles/**`, `/api/camera/mjpeg`, `/api/camera/rear/mjpeg`, `/api/settings` (GET).
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
| `/admin/version.json` | метка версии админки (не в git; пишет деплой — `tools/web-version.sh`), без входа; нет файла → 404 |
| `/display/**` | `web/display/` |

Статика отдаётся с `Cache-Control: no-cache` (браузер перепроверяет по ETag), `version.json` — с `no-store`.
Как админка по метке предлагает перезагрузиться после деплоя — `docs/contracts/remote.md`, раздел 6.

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
  Тоннель: фикс пропал, когда была карта и последняя скорость выше `map_above_mps`, — ещё `nav.dead_reckoning_s`
  (90 с, 0 — выкл.) от последнего фикса карта с `reason="dead_reckoning"` (экран везёт машину по счислению,
  display.md «Тоннель»), потом `no_fix_mode`.

## Тайлы

- `GET /tiles/{layer}/{z}/{x}/{y}` — `layer ∈ imagery|transport|places|street`. Порядок в URL Esri — `{z}/{y}/{x}` (см. `TileSettings`).
  Сначала дисковый кэш, иначе загрузка с Esri (таймаут 5 с, пул 8 соединений, `token=` из `esri_api_key`, если задан), сохранение, ответ.
  Ответ с `Cache-Control: no-store`: тайл и так в кэше ядра и в памяти экрана, а дисковый кэш Chromium на устройстве —
  в tmpfs, то есть вторая копия в памяти fnav-display. LRU-вытеснение по времени последнего доступа, лимит
  `storage.tile_cache_mb`. Индекс кэша строится фоновым обходом каталога: ядро не ждёт его при старте (камера,
  охрана, телеметрия, HTTP запускаются сразу), до конца обхода тайлы ищутся прямо на диске
  (`/api/tiles/stats` → `"ready": false`, размер и число — пока неполные).
  Ошибки (`Cache-Control: no-store`, клиент рисует родителя или заглушку):
  - `404` — тайла нет: Esri ответил 404/400 или серой заглушкой «Map data not yet available» (помним 10 мин), неизвестный слой; `400` — плохие координаты.
  - `503` + `Retry-After` — временно, повторить скоро: все соединения пула заняты, таймаут/обрыв/нет соединения, Esri ответил 5xx/429 (`Retry-After: 2`); нет интернета по последней проверке и тайла нет в кэше (`Retry-After: 15`).
  - `502` — Esri ответил не картинкой или другим кодом (например, 403 при неверном ключе).
  Клиент закрыл соединение (экран отменил тайл) — загрузка с Esri отменяется; докачиваются в кэш не больше 2 таких.
  `system.internet` — HEAD к Esri (раз в 60 с, без связи — раз в 15 с, после сетевой ошибки загрузки — через 1 с; полученный тайл тоже считается связью)
  отдельным соединением с таймаутом 4 с, не через пул тайлов: занятый медленными загрузками пул не означает «нет интернета».
- `GET /api/tiles/stats` → `{"size_mb":..,"count":..,"limit_mb":..,"hit_rate":..}`; `DELETE /api/tiles` — очистить.

## Записи

Файлы: `{recordings_dir}/YYYY-MM-DD/YYYY-MM-DD_HH-MM-SS.ts` (задняя камера — `{recordings_dir}/rear/YYYY-MM-DD/…`, путь
записи `rear/ДАТА/ИМЯ.ts`), защищено = рядом `…ts.lock`, индекс ключевых кадров — `…ts.idx`. Подробно — `camera.md`.

- `GET /api/recordings` → `[{"path":"2026-10-20/2026-10-20_16-40-00.ts","camera":"front"|"rear","date":"2026-10-20","start":"2026-10-20T16:40:00","start_ts":1792507200.123|null,"size_bytes":..,"duration_s":..|null,"protected":bool,"in_progress":bool,"events":[…],"marks":[{"kind":"motion"|"vehicle"|"person","ts":..,"offset_s":..,"dur_s":..}]}]` (обе камеры, новые сверху; `events` — события охраны (кроме `test`, `armed`, `disarmed`), `marks` — отметки движения без тревоги своей камеры, [vision.md](vision.md) «Отметки»).
  `start` — местное время устройства из имени файла (как в имени, без пояса); `start_ts` — истинное начало (unix, UTC)
  из `.ts.idx` регистратора (camera.md), `null` — записи до этой версии и `nosync/`. Пояс устройства меняется в дороге
  («Часовой пояс»), поэтому начало записи — `start_ts`, а без него — `start` в поясе устройства (не браузера!):
  так считают `offset_s` событий и отметок, порядок списка, и так же — админка (`web/admin/js/rec-play.js`
  `recStartMs`, `devtime.js`). У записи без `start_ts`, сделанной до смены пояса, начало сдвинуто на разницу поясов.
  Ссылка в плеер из журнала охраны и с главной: `#/recordings?play=<path>&t=<с от начала по имени>&at=<unix события>`;
  плеер берёт `at − start_ts`, если оба известны, иначе `t`.
- `GET /api/recordings/item/{path}` — одна запись в том же виде, что в списке (с `events` и `marks`), без обхода всех
  каталогов: плеер на странице охраны (кнопка Play у события, с 5 с до него); 404 — записи нет, 400 — плохой путь.
- `GET /api/recordings/file/{path}` — отдача файла с поддержкой `Range` (`video/mp2t`).
- `GET /api/recordings/hls/{path}.m3u8` (`…/2026-10-20/2026-10-20_16-40-00.ts.m3u8`) — плейлист HLS по записи
  (`application/vnd.apple.mpegurl`, `Cache-Control: no-cache`) для перемотки в любое место без докачки:
  `EXT-X-VERSION:4`, `PLAYLIST-TYPE:EVENT`, `EXT-X-START:TIME-OFFSET=0`; куски ~3 с (≤ 3 МБ) —
  `EXT-X-BYTERANGE` того же `.ts` по относительному адресу `../../file/{path}` (задняя — `../../../file/rear/…`;
  работает и под `/fnadmin/`),
  каждый начинается с PAT/PMT и ключевого кадра, `EXTINF` — точно по PTS. Запись закончена — `EXT-X-ENDLIST`;
  идёт запись — без него, только законченные куски (плеер перечитывает, старые куски не меняются).
  Ключевые кадры — из `.ts.idx` регистратора или поиском по файлу (camera.md). Не MPEG-TS — плейлист без кусков.
  Админка: Safari и iPhone — `video.src = m3u8` (свой HLS), остальные — hls.js (`web/admin/vendor/`).
- `POST /api/recordings/protect` `{"path":"...","protected":true}`; `POST /api/recordings/protect-current`.
- `DELETE /api/recordings/file/{path}` — запрещено для `in_progress` (`409`) и защищённых без `?force=1`;
  вместе с записью удаляются `.lock` и индекс `.ts.idx`.
- Защита от выхода за каталог (`..`, абсолютные пути) обязательна.

## Камера (прокси к fnav-camera `127.0.0.1:8081`, задняя — fnav-camera-rear `127.0.0.1:8083`)

Задняя камера — те же маршруты под `/api/camera/rear/…` (`status`, `mjpeg`, `snapshot.jpg`, `live.ts`, `restart`,
`whep` → MediaMTX `…/rear/whep`). Выключена (`rear_camera.enabled = false`) — `status` = `{"state":"disabled"}`,
остальное `503 "Rear camera is off…"`. В `status` обеих — `"camera":"front"|"rear"`, `"enabled"`, `"mock"`.

- `POST /api/camera/rear/credentials` `{"username"?: str, "password"?: str}` — логин в `settings.rear_camera.username`,
  пароль в `secrets.env` (`FNAV_REAR_CAMERA_PASSWORD`; `""` — удалить, нет поля — не менять), камере — `/reload`.
  → `{"ok":true,"username":…,"password_set":bool}`. Пароль наружу не отдаётся (в статусе задней — `password_set`).
- `GET /api/camera/view` → `{"camera":"front"|"rear","rear_enabled":bool}` — что показывает экран в режиме камеры;
  `POST /api/camera/view` `{"camera"?: "front"|"rear"}` (без поля — переключить; задняя выключена — `409`).
  Телеметрия: `display.camera`, `display.reason = "rear"`, `display.rear_mirror`; `camera_rear` — статус задней или `null`.
  Задний ход включает задний вид и сам (`settings.display.auto_rear_on_reverse`; ручной выбор главнее — display.md).
- `GET /api/camera/status` → `CameraStatus`; лимит записей — `protected_gb`, `loop_minutes`, `over_limit` (camera.md
  «Минимум петли»; админка предупреждает на «Camera» и «Recordings»).
- `GET /api/camera/mjpeg` → поток `multipart/x-mixed-replace` 768×576 (для экрана и превью).
- `GET /api/camera/snapshot.jpg` — текущий кадр; видео нет — 503 `{"detail": "No video from the camera"}` (не стоп-кадр).
- `POST /api/camera/whep` — прокси WebRTC WHEP к MediaMTX `http://127.0.0.1:8889/cam/whep` (тело SDP и заголовки `Content-Type`/`Location` проброшены; `PATCH`/`DELETE` по `Location` — тоже через `/api/camera/whep/...`).
- Кнопка на GPS-модуле: `press` (короткое) → передняя ↔ задняя камера на экране (как `camera_switch` клавиши 4; задняя
  выключена — длинный сигнал и подсказка); `long_press` (2 с) → защитить текущие записи обеих камер + событие WS + сигнал.
  Видеовыход на монитор (`POST /api/display/output`, wlr-randr у cage) — по API и действием `video_toggle`, если его
  назначить клавише.
- LED на модуле: `recording` → `blink_slow` (мигает), `error` → `blink_fast`, нет записи → `off`; после нажатия кнопки 3 с подтверждение: `on` (защищено) или `blink_fast` (ошибка).

## Датчики

- `GET /api/gps/satellites` → `[Satellite]`. `POST /api/gps/configure` → результат `hub.gps_configure()` (UBX:
  скорость порта, частота, созвездия, модель, Static Hold, NAV-PVT каждую эпоху — `fnav/sensors/m8n.py`).
- `Telemetry.gps.eps_mps` — ошибка скорости по оценке приёмника, м/с (TPV `eps` gpsd ← `sAcc` из UBX NAV-PVT): по ней
  слияние курса решает, насколько верить GPS-курсу. `null` — неизвестна: нет фикса, модуль не шлёт NAV-PVT (до
  «Reconfigure module»; порт медленнее 57600 при 10 Гц), значение > 10 м/с (без NAV-PVT gpsd выдумывает eps сам).
- `Telemetry.gps`: без фикса (`fix: "none"`) **`speed_mps` и `course_deg` — `null`**, в том числе когда TPV от gpsd
  не приходят дольше 3 с (gpsd перезапущен «Reconfigure module», упал, отошёл провод UART). `lat`/`lon` при этом
  могут остаться — это последняя известная позиция, не текущая. Потребители понимают `null` как «скорость неизвестна»:
  экран — `--` и без «превышения», веб — `—`, режим экрана — `nav.no_fix_mode`, камера держит прежнее решение
  «едем/стоим» (`MotionState`), игры не закрываются «на ходу», охрана и уровень без фикса GPS не используют.
- **Привязка компаса к GPS** (`settings.heading.anchor_to_gps`, `Heading.anchor_offset_deg`): на ходу хаб запоминает
  поправку «GPS-курс − компас», на стоянке курс = компас + поправка. Поправка верна только для той модели компаса, под
  которой выучена (действующая калибровка — offset/matrix/deviation, `mount_offset_deg`, `chip`/`axes`, склонение):
  при любой их смене (ручная калибровка, сброс, новая автоподгонка, правка настроек, другие оси от `chip=auto`) она
  сбрасывается вместе с копией в `sensors_state.json` и на ходу выучивается заново. В файле поправка хранится с
  отпечатком модели (`anchor_model`); после перезапуска с другой действующей калибровкой (например, поправка выучена
  под несохранённой автоподгонкой) или без отпечатка (старый файл) она не применяется.
- `POST /api/compass/calibration/start|finish|cancel`, `GET /api/compass/calibration` → `CalibrationStatus`.
  `finish` сохраняет результат в `settings.compass.calibration` и возвращает его.
- **Автокалибровка по GPS-курсу** (на ходу, `settings.compass.auto_calibrate`): хаб сам собирает точки и вызывает `on_calibration_update(cal)`; fnav-core сохраняет `cal` в `settings.compass.calibration`. Состояние — `Telemetry.compass_cal` (`AutoCalStatus`).
  `POST /api/compass/calibration/reset-auto` — сбросить накопленные точки (сохранённую калибровку не трогает).

## Охрана и уровень (MPU-6050)

Полностью — в отдельных контрактах, здесь только перечень:

- **Охрана** — [alarm.md](alarm.md) §4: `GET /api/alarm`, `POST /api/alarm/arm|disarm|silence|test|hazard/test`,
  `GET /api/alarm/snapshot/{id}.jpg` (задней камеры — `{id}-rear.jpg`; в событии `data.snapshot_rear`, `data.segment_rear`; `?thumb=1` — миниатюра ~5 КБ для списков), `DELETE /api/alarm/events`, `GET /api/alarm/marks?limit=50` / `DELETE /api/alarm/marks` (отметки движения и громких звуков без тревоги — [vision.md](vision.md) «Отметки», «Громкие звуки»), `GET /api/alarm/sound/{имя}.wav` (клип громкого звука; у записей задней камеры поле `audio` — есть ли в файле звук), `POST /api/alarm/simulate` и `POST /api/alarm/simulate/mark` (только `--mock`, иначе 404). Настройки — `settings.alarm`,
  телеметрия — `Telemetry.alarm`, клавиша — действие `"alarm"`. Камера на охране (fnav-watch) — [vision.md](vision.md).
- **Уровень** — [level.md](level.md): `GET /api/level`, `POST /api/level/calibration/reference|forward`,
  `DELETE /api/level/calibration`. Настройки и калибровка — `settings.level`, телеметрия — `Telemetry.level`.

## Система

- `GET /api/system/status` → `{"system": SystemStatus, "disk": DiskStatus, "services": {"fnav-camera":"active",...}, "version":"..."}`.
  В `SystemStatus` (и в телеметрии) — `timezone` (IANA, `null` — не известен) и `utc_offset_min` пояса устройства.
- `GET /api/system/logs?unit=fnav-core&lines=200` → `{"lines":[...]}` (`journalctl`, только `fnav-*`, `mediamtx`, `gpsd`).
- `POST /api/system/reboot`, `POST /api/system/shutdown`.
- `GET /api/system/wifi` → `[{"ssid","priority","active","signal_pct"}]`; `POST /api/system/wifi` `{"ssid","password","priority"}`; `DELETE /api/system/wifi/{ssid}` (через `nmcli`).
- `POST /api/system/password` `{"old","new"}` (новый ≥ 6 символов).
- В mock-режиме системные действия не выполняются, возвращают `{"ok":true,"mock":true}`.

### Часовой пояс

Имена записей, время в push («Hit 0.42 g at 14:03») и часы на экране — по местному времени системы; пояс ставится
при установке (`FNAV_TIMEZONE`) и дальше **следует за машиной** (`fnav/core/timezone.py`, настройка
`settings.system.auto_timezone`, по умолчанию `true`, страница System админки):

- Open-Meteo (`fnav/core/weather.py`, `timezone=auto`) в каждом прогнозе называет пояс точки (`timezone`, IANA).
  Если свежий прогноз **для живой позиции GPS** (не для последней точки из кэша) два раза подряд называет пояс,
  отличный от системного, fnav-core ставит его: `timedatectl --no-ask-password set-timezone <пояс>` (право — правило
  polkit `pi/polkit/50-fnav-timedate.rules`, только это действие и только для `fnav`), затем `time.tzset()`; смена —
  в журнале `fnav-core`. Два подряд — чтобы у границы пояс не «дребезжал»; прогноз обновляется раз в 30 мин или
  через 25 км, так что смена — через ~30–60 мин после границы. `Etc/*`, `GMT`, `UTC` (море, паром) не ставятся.
- Остальные процессы замечают смену сами: fnav-core и камеры проверяют `/etc/localtime` (`fnav/tz.py` `ZoneWatch`:
  статус системы раз в 5 с, обслуживание камеры ≤ 10 с) и вызывают `time.tzset()`. fnav-watch местное время не
  форматирует. Часы экрана — по `SystemStatus.utc_offset_min` (display.md).
- Админка (телефон может быть в другом поясе) переводит местное время устройства (имена записей) по
  `timezone` из `/api/session` и телеметрии (`web/admin/js/devtime.js`, Intl, с переходами на летнее время;
  время, которое при переводе часов назад бывает дважды, — первое). Пояс не известен (старое устройство, mock на
  Windows) — пояс браузера, как раньше. Mock-сервер админки: `--tz Europe/Lisbon` — устройство в другом поясе.
- В `--mock` пояс системы не трогается.

## Ошибки

`{"detail": "message in English"}` + код (`400/401/404/409/422/429/500/503`; 429 + `Retry-After` — блокировка после 5 неудачных входов).
