# Контракт: HTTP / WebSocket API fnav-core

Базовый адрес: `http://fnav.local/` (dev: `http://127.0.0.1:8000/`). Все JSON — UTF-8. Модели — `fnav/models.py`.

## Авторизация

- `POST /api/login` `{"password": "..."}` → `200 {"ok": true}` + cookie `fnav_session` (HttpOnly, SameSite=Lax, 30 дней). Неверно → `401`.
  Защита от перебора: после 5 неудачных попыток с IP — пауза 30 с.
- `POST /api/logout` → удаляет cookie. `GET /api/session` → `{"authenticated": bool, "local", "remote", "mock"}`;
  со входом ещё `"timezone": "Europe/Warsaw"|null` — пояс устройства (см. «Часовой пояс»; без входа не отдаётся:
  пояс ≈ страна, где машина) и `"guest": bool`; у гостя (см. «Гостевой доступ») `authenticated: true`, `guest: true`
  и `"guest_expires"` (unix) — до когда действует его ссылка.
- Пароль хранится как хэш `hashlib.scrypt` в `secrets.env` (`FNAV_PASSWORD_HASH=scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`), секрет подписи сессий — `FNAV_SESSION_SECRET`.
- Мусор в cookie (в том числе не-ASCII подпись) — просто неверная сессия (`401`), не `500`.
- **Без авторизации доступно:** `/api/login`, `/api/session`, `/api/guest/login`, `/admin/login.html` и статика логина;
  а с адреса `127.0.0.1` (киоск Chromium на самом устройстве) — также `/display/**`, `/ws`, `/tiles/**`, `/api/camera/mjpeg`, `/api/camera/rear/mjpeg`, `/api/settings` (GET).
- Всё остальное без сессии → `401` (API) или редирект на `/admin/login.html` (страницы).
- Установленное приложение (PWA, `display-mode: standalone` / iOS Home Screen) при первом запуске на устройстве
  спрашивает пароль, даже если браузер уже вошёл (на Android приложение делит cookie с Chrome): метка
  `fnav.appLogin` в localStorage ставится только входом из самого приложения и снимается кнопкой Log out
  (`web/admin/js/app-login.js`). Это клиентская ступенька; защита — по-прежнему сессия на сервере.

## Гостевой доступ (только просмотр)

Владелец даёт ссылку, по которой другой человек видит **всю админку, как владелец, но ничего не может изменить**.
Код: `fnav/core/guests.py` (ссылки), `fnav/core/auth.py` (роль сессии и запрет), `fnav/core/ws.py` (команды WS).

- Ссылка: `<адрес админки>admin/login.html#guest=<токен>` — на устройстве `http://<ip>/admin/…`, через сервер
  `https://<сервер>/fnadmin/admin/…` (токен один и тот же). Токен — во фрагменте (`#`): в адресе запроса и журналах
  nginx его нет. Страница входа сразу убирает его из адресной строки (`history.replaceState`) и меняет на cookie:
  `POST /api/guest/login` `{"token": "..."}` → `200 {"ok":true,"guest":true,"expires":…}` + cookie `fnav_session`
  (гостевая, `Max-Age` — до конца срока ссылки); неверный, отозванный или истёкший токен → `401`. Анти-перебор —
  тот же счётчик, что у входа паролем (5 ошибок с IP → `429` на 30 с; удачная гостевая попытка счётчик не сбрасывает),
  через сервер ещё `limit_req` nginx. Пока устройство не в сети, ссылку не обменять (`503 Device is offline`).
- Гостевая cookie: `g1.<exp>.<id ссылки>.<sig>`, подпись — тем же ключом, что у сессий владельца
  (HMAC(секрет сессий, хэш пароля)): смена пароля сбрасывает и гостевые cookie (ссылку можно открыть снова).
  Действует, пока не вышел её срок **и** ссылка есть в списке (отзыв = удаление — сразу).
- Ссылки — `{data_dir}/guest_links.json` (атомарная запись, 0600): `id`, `label`, `created`, `expires`, `last_used`
  и **только SHA-256 токена**; сам токен показывается владельцу один раз, в ответе на создание. Истёкшие удаляются
  при следующей записи; `last_used` пишется на диск не чаще раза в 5 мин. Ссылок не больше 50.
- Только владелец (гостю `403`):
  - `GET /api/guest/links` → `{"links":[{"id","label","created","expires","last_used"}],"remote_origin":…,"now":…}`
    (действующие, новые первыми; `now` — часы устройства, `remote_origin` — `https://<сервер>` удалённой админки,
    если её открывали: тогда и с Wi-Fi машины показывается ссылка «через сервер»);
  - `POST /api/guest/links` `{"label"?: str ≤ 64, "expires_in_s": 600…31536000}` → то же + `"link"`, `"token"`,
    `"path": "admin/login.html#guest=<токен>"`; админка: 1 день / 7 дней / своя дата (страница System);
  - `DELETE /api/guest/links/{id}` → отозвать (`404` — нет такой).
- **Запрет — на сервере, по методу, а не по списку маршрутов** (`AuthMiddleware`): гостю можно `GET`/`HEAD`/`OPTIONS`
  и явный короткий список (`auth.guest_allowed`): живое видео WebRTC — `POST /api/camera[/rear]/whep` (предложение
  SDP), `PATCH`/`DELETE /api/camera[/rear]/whep/…` (ICE, закрыть свою сессию), и `POST /api/logout` (удаляет cookie
  только у самого гостя). Всё остальное — `403 {"detail":"Read-only guest access"}`, в том числе маршруты, которых
  ещё нет: новый `POST` закрыт для гостя сам (тест обходит все маршруты приложения). Гость не создаёт ссылки, не
  меняет пароль, не подписывается на push (это fnav-relay, remote.md), не выходит «за владельца».
- WebSocket: гость подключается, получает то же, но настройки — без секретов; из команд — только чтение (`subscribe`,
  `unsubscribe`, `ping`), на любую другую — `{"type":"error","status":403,"detail":"Read-only guest access",
  "command":"…"}` (новые команды для гостя закрыты, пока их не добавят в `ws.READ_COMMANDS`).
- Секреты гостю не отдаются (`guests.redact_settings`): в `GET /api/settings` и сообщении WS `settings` пустой
  `tiles.esri_api_key` и без `логин:пароль@` в `rear_camera.url`/`lores_url`. Остальное и так без секретов: пароли
  Wi-Fi, пароль задней камеры (`password_set`), хэш пароля, секрет сессий, токен устройства на сервере, endpoint'ы
  push-подписок наружу не отдаются никому.
- Админка (`web/admin/js/guest.js`): бейдж и баннер «Guest — read only», кнопки действий (`.btn.primary`,
  `.btn.danger`, отправка форм, поля паролей) выключены, полоса «Save» скрыта. Это только вид — решает сервер.

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
- `{"type":"ping"}` → `{"type":"pong"}` (в общей очереди с телеметрией).
- Гостю (только просмотр) — только эти; на остальные — `{"type":"error","status":403,…}` (см. «Гостевой доступ»).

Живость связи в админке (`web/admin/js/store.js`, правила — `conn-fmt.js`): пинг раз в 5 с; открытый сокет, от
которого 4 с не пришло ни одного сообщения (телеметрия идёт 10 Гц, через туннель 2 Гц), закрывается и открывается
заново, а «подключающийся» дольше 10 с — бросается. Возврат на экран (`visibilitychange`, `pageshow`, заметно
опоздавший таймер — страница была заморожена) — проба: за 1,5 с должно прийти хоть что-то, иначе новый сокет;
событие `online` (сменилась сеть) — новый сокет сразу. Так iPhone после блокировки экрана и полумёртвый туннель
(ssh на сервере ещё не понял, что Pi нет) не держат «нет данных» до таймаута TCP (~90 с). Состояние «онлайн» — с
гистерезисом: офлайн после 6 с без телеметрии (время, пока страница скрыта, не считается), снова онлайн после 1,5 с
ровной телеметрии (первый раз — сразу); «Connection lost / restored» на слабой мобильной связи не идут парами.
REST-запросы админки — с таймаутом (`api.js` `requestTimeout`: 15 с, дольше — Bluetooth, Wi-Fi, настройка GPS,
push test, журнал и т.п.) и понятной ошибкой «No answer from the device in 15 s — the connection is slow or down»
(nginx сам ждёт 60 с).

## Настройки

- `GET /api/settings` → `Settings` (гостю — без секретов, см. «Гостевой доступ»). `GET /api/settings/defaults` →
  `Settings()` по умолчанию.
- `PATCH /api/settings` — частичное глубокое слияние `{"display":{"orientation":"north_up"}}` → валидированный полный `Settings`; `422` с описанием при ошибке.
- `PUT /api/settings` — полная замена. `POST /api/settings/reset` — сброс к умолчаниям.
- После сохранения: атомарная запись `settings.json`, рассылка по WS, `hub.apply_settings()`, `POST 127.0.0.1:8081/reload` (ошибку недоступности камеры игнорировать).
- Последняя удачная копия — `settings.json.lkg` (после каждой удачной загрузки, если отличается, и каждого
  сохранения). Битый `settings.json` (не JSON, не проходит проверку, не читается) → настройки из копии (битые секции —
  из копии, без неё — умолчания), исходник — в `settings.json.bad-<ГГГГММДД-ЧЧММСС>` (хранятся 3 новейших, одинаковые
  не дублируются), и файл сразу переписывается восстановленным — следующий запуск читает целый файл. Не читается
  (права) и копии нет — файл не трогается до первого сохранения. Все файлы — 0600, владелец — пользователь сервиса
  (`fnav:fnav`); чужой владелец (root) = «не читается».

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
записи `rear/ДАТА/ИМЯ.ts`), защищено = рядом `…ts.lock`, индекс ключевых кадров — `…ts.idx`, трек GPS — `…ts.gps`.
Подробно — `camera.md`.

- `GET /api/recordings` → `[{"path":"2026-10-20/2026-10-20_16-40-00.ts","camera":"front"|"rear","date":"2026-10-20","start":"2026-10-20T16:40:00","start_ts":1792507200.123|null,"size_bytes":..,"duration_s":..|null,"protected":bool,"in_progress":bool,"events":[…],"marks":[{"kind":"motion"|"vehicle"|"person"|"sound"|"impact","ts":..,"offset_s":..,"dur_s":..}]}]` (обе камеры, новые сверху; `events` — события охраны (кроме `test`, `armed`, `disarmed`), `marks` — отметки движения без тревоги своей камеры, [vision.md](vision.md) «Отметки», и удары на ходу — `"kind":"impact"` с `peak_g`, у записей обеих камер, camera.md «Удар на ходу»).
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
- `GET /api/recordings/gps/{path}` — трек GPS записи (`…ts.gps`, camera.md «Трек GPS записи»): `{"path": …, "samples":
  [{"t": unix, "lat", "lon", "alt": м|null, "speed": м/с|null, "course": °|null, "fix": "2d"|"3d"}]}` по времени (~1 в
  секунду; нет трека — `[]`; 404 — нет записи, 400 — плохой путь). Плеер ищет точку по unix-времени: начало записи
  (`start_ts`, без него — как выше) + позиция воспроизведения (`web/admin/js/rec-drive.js`). `?format=gpx` — файл GPX
  1.1 (`application/gpx+xml`, вложение `ИМЯ.gpx`, задняя — `rear-ИМЯ.gpx`; скорость и курс — Garmin
  TrackPointExtension v2, перерыв > 10 с — новый `trkseg`).
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
- `in_progress` (и «идёт запись» у HLS и `DELETE`) — `current_segment` передней **или задней** из статусов их сервисов,
  либо файл менялся меньше 3 с назад (camera.md «Хранилище» у задней).
- `DELETE /api/recordings/file/{path}` — запрещено для `in_progress` (`409`) и защищённых без `?force=1`;
  вместе с записью удаляются `.lock`, индекс `.ts.idx` и трек `.ts.gps`.
- Удар на ходу (MPU-6050, `settings.impact`) защищает текущие записи обеих камер, как `protect-current`, и оставляет
  метку `impact` — camera.md «Удар на ходу».
- Защита от выхода за каталог (`..`, абсолютные пути) обязательна.

## Камера (прокси к fnav-camera `127.0.0.1:8081`, задняя — fnav-camera-rear `127.0.0.1:8083`)

Задняя камера — те же маршруты под `/api/camera/rear/…` (`status`, `mjpeg`, `snapshot.jpg`, `live.ts`, `restart`,
`whep` → MediaMTX `…/rear/whep`). Выключена (`rear_camera.enabled = false`) — `status` = `{"state":"disabled"}`,
остальное `503 "Rear camera is off…"`. В `status` обеих — `"camera":"front"|"rear"`, `"enabled"`, `"mock"`.
`discovered_host` в статусе задней (камера нашлась по MAC на новом адресе) — ядро записывает хост в
`rear_camera.url`/`lores_url` (camera.md «Поиск по MAC»).

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
  Учится поправка только по надёжному GPS-курсу: полное доверие (ниже), вперёд (не задний ход и не «курс напротив»),
  TPV свежий (< 0,3 с), на прямой (|dкурс/dt| < 3 °/с по GPS-курсу за ~1 с и по гироскопу).
- **Доверие GPS-курсу**: есть точность скорости `GpsFix.eps_mps` (gpsd `eps`, u-blox NAV-PVT sAcc) — курс полностью по
  GPS выше max(1 м/с, 3·eps), переход — на `heading.blend_band_mps` ниже; нет — как раньше, по `heading.gps_above_mps`
  и `blend_band_mps` (3,5 м/с). TPV старше 0,5 с (тоннель) в курс не идёт. Автокалибровка не берёт точку при
  eps > v·tan 4° и при наклоне машины > 3°.
- **Гироскоп** (MPU-6050, поток чтения общий с охраной и уровнем: `ImuService.subscribe_motion`, хабу его даёт fnav-core —
  `attach_imu`). Рысканье = −(ω − смещение нуля)·U (°/с, > 0 — вправо), U — «вверх» калибровки уровня (без неё — по
  тяжести). Смещение нуля — среднее на стоянке (GPS < 0,3 м/с, гироскоп и акселерометр тихие ≥ 2 с), до первой стоянки —
  сохранённое (`gyro_bias` в `sensors_state.json`). Курс: каждый цикл += поворот по гироскопу; подтяжка к GPS-курсу
  τ = 1 с (разница — с курсом момента эпохи GPS: ~0,15 с + возраст TPV, иначе на повороте курс тянется назад), к компасу
  (с поправкой привязки) — τ = 20 с на малой скорости и без GPS, 120 с на стоянке; компас ушёл от гироскопа > 6° за 0,5 с
  (грузовик, трамвай, инвертор) — 3 с его не слушаем. Остаток смещения по рысканью учится по GPS на прямой.
  Без гироскопа (mock, датчика нет или он молчит > 0,5 с, смещение ещё не известно) — как раньше: смешивание компас/GPS
  и `heading.smoothing_s`; переход в обе стороны — без скачка. `Heading.yaw_rate_dps` — рысканье, `null` без гироскопа.
- **Компенсация наклона компаса** (`Heading.tilt_comp`): с калибровкой уровня по эталону ([level.md](level.md)) вектор
  «вверх» по акселерометру (за вычетом разгона dv/dt по GPS и центробежного v·рысканье) переводится в оси модуля компаса
  (повёрнут относительно машины на −(`mount_offset_deg` + A девиации)), курс — по горизонтальной проекции поля. Нужна ось Z
  компаса: 3D-калибровка или смещение z по WMM — на ровной дороге z = |горизонт|·Z/H (ручная 2D-калибровка сохраняет его
  в `offset[2]`, для автокалибровки хаб считает его по накопленным точкам). Без свежей скорости GPS — только пока датчик
  неподвижен (в тоннеле на повороте центробежное выглядит как крен).
- **Задний ход** — `SensorSnapshot.reversing` (по нему fnav-core показывает заднюю камеру). `true`, когда по сырому TPV
  (без фильтра «стоим», не старше 0,5 с) скорость 0,4…5 м/с (и ≥ 2·eps) направлена против курса машины
  (|GPS-курс − курс| ≥ 110°) подряд: 0,4 с, если и акселерометр видел трогание назад (продольная «скорость» с трогания
  ≤ −0,25 м/с, нужна калибровка «вперёд»), 1 с — курс по гироскопу, 1,5 с — только по компасу. Курс машины — не
  подтянутый к этому GPS-курсу и сверенный (поправка привязки, автокалибровка, гироскоп после GPS), иначе по GPS не судим.
  Не включается: акселерометр видел трогание вперёд (≥ 0,25 м/с), датчик движения неподвижен ≥ 2 с (GPS «едет» на
  стоянке), крутой поворот на малой скорости (ω·2 м > 0,7·v), курс поперёк (70…110°). `false`: стоим (< 0,3 м/с) или едем
  вперёд (≤ 70°) 0,5 с подряд, быстрее 5 м/с — сразу. Пока задний ход (и пока на малой скорости GPS-курс почти напротив
  сверенного курса) GPS-курс в курс не идёт, поправка привязки и автокалибровка не учатся: курс — по компасу и гироскопу.
- `POST /api/compass/calibration/start|finish|cancel`, `GET /api/compass/calibration` → `CalibrationStatus`.
  `finish` сохраняет результат в `settings.compass.calibration` и возвращает его.
  «Reset calibration» в админке — `reset-auto` и `PATCH` всей `CompassCalibration` по умолчанию (`/api/settings/defaults`):
  `PATCH` сливает объекты, частичный сброс оставлял девиацию, `source` и цифры качества.
- **Автокалибровка по GPS-курсу** (на ходу, `settings.compass.auto_calibrate`): хаб сам собирает точки и вызывает `on_calibration_update(cal)`; fnav-core сохраняет `cal` в `settings.compass.calibration`. Состояние — `Telemetry.compass_cal` (`AutoCalStatus`).
  `POST /api/compass/calibration/reset-auto` — сбросить накопленные точки (сохранённую калибровку не трогает).

## Охрана и уровень (MPU-6050)

Полностью — в отдельных контрактах, здесь только перечень:

- **Охрана** — [alarm.md](alarm.md) §4: `GET /api/alarm` (`?events=0` — без журнала), `GET /api/alarm/events`,
  `POST /api/alarm/arm|disarm|silence|siren|test|hazard/test`,
  `GET /api/alarm/snapshot/{id}.jpg` (задней камеры — `{id}-rear.jpg`; в событии `data.snapshot_rear`, `data.segment_rear`; `?thumb=1` — миниатюра ~5 КБ для списков), `DELETE /api/alarm/events`, `GET /api/alarm/marks?limit=50` / `DELETE /api/alarm/marks` (отметки движения и громких звуков без тревоги — [vision.md](vision.md) «Отметки», «Громкие звуки»), `GET /api/alarm/sound/{имя}.wav` (клип громкого звука; у записей задней камеры поле `audio` — есть ли в файле звук), `POST /api/alarm/simulate` и `POST /api/alarm/simulate/mark` (только `--mock`, иначе 404). Настройки — `settings.alarm`,
  телеметрия — `Telemetry.alarm`, клавиша — действие `"alarm"`. Камера на охране (fnav-watch) — [vision.md](vision.md).
- **Уровень** — [level.md](level.md): `GET /api/level`, `POST /api/level/calibration/reference|forward`,
  `DELETE /api/level/calibration`. Настройки и калибровка — `settings.level`, телеметрия — `Telemetry.level`.

## Система

- `GET /api/system/status` → `{"system": SystemStatus, "disk": DiskStatus, "services": {"fnav-camera":"active",...}, "version":"..."}`;
  `services` — `fnav-core`, `fnav-camera`, `fnav-camera-rear`, `fnav-watch`, `fnav-display`, `fnav-radio`,
  `fnav-tunnel` (без `/etc/fnav/remote.env` — `inactive`), `mediamtx`, `gpsd`, `chrony`.
  В `SystemStatus` (и в телеметрии) — `timezone` (IANA, `null` — не известен) и `utc_offset_min` пояса устройства.
- `GET /api/system/logs?unit=fnav-core&lines=200` → `{"lines":[...]}` (`journalctl`, только `fnav-*`, `mediamtx`, `gpsd`).
- `POST /api/system/reboot`, `POST /api/system/shutdown`.
- `GET /api/system/wifi` → `[{"ssid","priority","active","signal_pct"}]`; `POST /api/system/wifi` `{"ssid","password","priority"}`;
  `DELETE /api/system/wifi?ssid=<SSID>` (через `nmcli`). SSID — параметром запроса: `/` в пути (`%2F`) раскодируется
  до маршрутизации; прежний `DELETE /api/system/wifi/{ssid}` оставлен для SSID без `/`.
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

`{"detail": "message in English"}` + код (`400/401/403/404/409/422/429/500/503`; 429 + `Retry-After` — блокировка после 5 неудачных входов;
403 `Read-only guest access` — гостю нельзя менять).
