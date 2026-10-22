# Контракт: удалённый доступ (<server>/fnadmin), журнал позиций (Tracking), живое видео по требованию

Тексты интерфейса и ответов API — на английском. Комментарии в коде — как в остальном проекте (русский).

## Схема

```
браузер ── https://<server>/fnadmin/… ──► nginx (сервер)
                                                 ├─ /fnadmin/admin/, /fnadmin/display/  → статика (копия web/ на сервере)
                                                 ├─ /fnadmin/api/tracking/…             → fnav-relay 127.0.0.1:8095
                                                 ├─ /fnadmin/api/{login,logout,session} → Pi; если Pi не в сети → fnav-relay
                                                 └─ /fnadmin/api/…, /fnadmin/ws, /fnadmin/tiles/… → 127.0.0.1:18080 ─┐
                                                                                                                      │ ssh -R
Pi (fnav-tunnel.service: ssh -N -R 127.0.0.1:18080:127.0.0.1:8090 -L 127.0.0.1:18095:127.0.0.1:8095 fnavtun@сервер) ◄┘
   ├─ fnav-core слушает 0.0.0.0:80 (локально) и 127.0.0.1:8090 («удалённый» вход: без доверия к localhost)
   └─ журнал: fnav-core раз в минуту берёт GPS-точку → outbox (SQLite) → POST http://127.0.0.1:18095/device/samples
```

Сервер без префикса снимает `/fnadmin`: `/fnadmin/api/x` → Pi `/api/x`. `/fnadmin` и `/fnadmin/` → 301 на `/fnadmin/admin/`.

## 1. Префикс адресов (веб: admin и display)

- Все адреса приложения строятся от корня `ROOT`:
  `ROOT = location.pathname.replace(/\/(admin|display)\/.*$/, "") + "/"` → на устройстве `"/"`, удалённо `"/fnadmin/"`.
  `REMOTE = ROOT !== "/"`.
- Никаких абсолютных `"/api/…"`, `"/ws"`, `"/tiles/…"`, `"/display/…"`, `"/admin/…"` в коде: только `ROOT + "api/…"` и т.п.
  (WebSocket: `${proto}//${location.host}${ROOT}ws`).
- Логин: `goLogin()` передаёт `next` относительно ROOT (например `admin/#/camera`); `login.js` переходит на `ROOT + next`,
  принимая только относительные пути без схемы, `//` и `..`.
- `GET api/session` может вернуть `{"authenticated":true,"offline":true}` — это ответ сервера, когда Pi не в сети.
  Тогда админка показывает баннер «Device is offline» и по умолчанию открывает Tracking; остальные страницы показывают
  ошибки связи как обычно.
- Удалённо (REMOTE) экран (display, в т.ч. в превью Screen) берёт тайлы Esri **напрямую** с arcgisonline (как демо-режим),
  а не через `/tiles/` устройства, а MJPEG камеры — с `?fps=5`.

## 2. Сервер: fnav-relay (`server/fnav_relay/`, только стандартная библиотека Python 3.12)

- Слушает `127.0.0.1:8095`. Путь принимается и с префиксом `/fnadmin`, и без него (префикс отрезается).
- Конфиг: `/etc/fnav-relay/relay.env` (`FNAV_RELAY_DEVICE_TOKEN`, `FNAV_RELAY_DB=/var/lib/fnav-relay/track.db`,
  `FNAV_RELAY_RADIUS_M=50`, `FNAV_RELAY_STOP_MIN_S=300`, `FNAV_RELAY_LISTEN=127.0.0.1:8095`,
  `FNAV_RELAY_COOKIE_PATH=/fnadmin/`).
- IP клиента для анти-перебора — из `X-Real-IP` (ставит nginx).

### Авторизация
- Cookie `fnav_session`, формат токена **точно как в `fnav/core/auth.py`** (`v1.<exp>.<nonce>.<sig>`,
  ключ = HMAC-SHA256(session_secret, password_hash)). Пароль — `scrypt$n$r$p$salt$hash` как в `fnav/core/secretsfile.py`.
- Хэш пароля и секрет сессий присылает устройство (`POST /device/auth`), сервер хранит их у себя (файл 0600 или таблица).
- Устройство: заголовок `X-Fnav-Device-Token: <token>` (сравнение `hmac.compare_digest`).
- Эндпоинты `/api/tracking/*` — с валидной cookie **или** с токеном устройства. `/device/*` — только с токеном.

### Эндпоинты
| Метод | Путь | Что |
|---|---|---|
| POST | `/device/auth` | `{"password_hash":"scrypt$…","session_secret":"…"}` → `{"ok":true}` |
| POST | `/device/samples` | `{"samples":[{"ts":1761140000.0,"lat":..,"lon":..,"speed_mps":..,"course_deg":..,"hdop":..,"fix":"3d"}]}` по возрастанию `ts` → `{"ok":true,"accepted":N,"last_ts":…}`. Точки с `ts ≤` последней обработанной молча пропускаются (повторная отправка безопасна). |
| POST | `/api/login` | `{"password":"…"}` → 200 + Set-Cookie (`Path=/fnadmin/; Secure; HttpOnly; SameSite=Lax; Max-Age=30 дней`); 401 `{"detail":"Wrong password"}`; 429 после 5 ошибок (30 с); 503 если хэш ещё не прислан |
| POST | `/api/logout` | удалить cookie |
| GET | `/api/session` | `{"authenticated":bool,"local":false,"mock":false,"offline":true}` |
| GET | `/api/tracking/places?from=&to=` | `{"places":[{"id","lat","lon","kind":"stop"\|"transit","visits","first_ts","last_ts","longest_s","total_s"}],"stop_min_s":300,"radius_m":50}` — места, у которых есть визиты, пересекающие [from,to] (unix-секунды, по умолчанию — всё время); поля посчитаны по визитам в диапазоне |
| GET | `/api/tracking/places/{id}` | `{"place":{…},"visits":[{"start_ts","end_ts","duration_s","samples"}]}` — все визиты, новые первыми |
| GET | `/api/tracking/track?from=&to=` | `{"points":[[start_ts,lat,lon],…]}` — визиты по времени (линия маршрута) |
| GET | `/api/tracking/last` | последняя принятая точка `{"ts","lat","lon","speed_mps","course_deg","received_ts"}` или `{}` |
| * | любой другой `/api/…` | 503 `{"detail":"Device is offline"}` (сервер отвечает за Pi, когда того нет в сети) |
| GET | `/healthz` | `{"ok":true}` без авторизации (проверка при установке) |

Неверный токен устройства → 403; `/api/tracking/*` без cookie и токена → 401; вход до `/device/auth` → 503.

### Журнал (слияние точек)
- Хранятся только **места** и **визиты** (одинаковые позиции не пишутся): `places(id, lat, lon, created_ts)`,
  `visits(id, place_id, start_ts, end_ts, samples)`, плюс последняя точка и `last_ts` в служебной таблице.
- Новая точка (в порядке `ts`):
  1. если есть текущий (последний) визит и расстояние до его места ≤ `radius_m` → `end_ts = ts`, `samples += 1`;
  2. иначе ближайшее место в пределах `radius_m` (поиск по bbox + haversine) → новый визит в этом месте;
  3. иначе новое место (координаты первой точки) + новый визит.
- Перерыв в данных (Pi был выключен) не разрывает визит, если точка снова в том же месте.
- `kind = "stop"`, если самый длинный визит (в диапазоне) ≥ `stop_min_s`, иначе `"transit"`.

## 3. Устройство: fnav-core (делает ведущий)

- Настройки `tracking`: `enabled=True`, `interval_s=60`, `max_hdop=5.0`. Точка пишется, если фикс 2d/3d, `hdop ≤ max_hdop`,
  `age_s < 5`. Outbox: `/var/lib/fnav/track_outbox.db`; отправка пачками (≤ 500) при наличии туннеля.
- `/api/tracking/*` на устройстве — прокси на fnav-relay через `-L` (с токеном устройства); если сервер недоступен —
  503 `{"detail":"Tracking server is unreachable"}`.
- Вход `127.0.0.1:8090`: localhost-доверие (киоск) не действует, IP клиента — из `X-Real-IP`;
  `api/session` отвечает `"remote": true`; телеметрия по WebSocket — 2 Гц (каждый 5-й кадр) вместо 10.
- `GET /api/remote/status` — `{"configured","outbox","last_sample_ts","last_upload_ts","last_error"}`.
- `/etc/fnav/remote.env`: `FNAV_RELAY_URL=http://127.0.0.1:18095`, `FNAV_RELAY_DEVICE_TOKEN=…`, параметры туннеля.

## 4. Живое видео по требованию (камера)

- fnav-camera: `GET /live.ts` (127.0.0.1:8081) → `video/mp2t`, поток MPEG-TS **отдельного** аппаратного H.264 с lores
  (768×576, `camera.fps`, битрейт `camera.live_bitrate_kbps`, по умолчанию 800, GOP = 1 с). Второй энкодер запускается
  при первом клиенте и останавливается через ~5 с после ухода последнего — нет зрителей, нет кодирования и трафика.
  Поток начинается с IDR с SPS/PPS. Запись (main) не прерывается и не меняется. Если второй энкодер не стартует → 503.
- `GET /mjpeg?fps=N` — прореживание кадров (1…30; без параметра — как сейчас).
- fnav-core: `GET /api/camera/live.ts` и `GET /api/camera/mjpeg?fps=` — потоковый прокси (без буферизации).
- Админка, Camera: режимы `webrtc` | `stream` (mpegts.js, `isLive`, догон задержки) | `mjpeg`.
  По умолчанию: на устройстве — `webrtc`, удалённо — `stream` (WebRTC через туннель не работает).
  Поток идёт только пока открыта страница: при уходе со страницы / закрытии превью плеер уничтожается.
