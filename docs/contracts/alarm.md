# Контракт: охрана (датчик удара MPU-6050, наклон, сдвиг по GPS) и push-уведомления

Тексты интерфейса, уведомлений и ответов API — на английском. Комментарии в коде — как в остальном проекте.

## Схема

```
MPU-6050 ─I2C-1─► fnav-core: AlarmService ──► событие ─┬─► журнал (data_dir/alarm_events.json), Telemetry.alarm
GPS (gpsd) ──────►   (охрана вкл/выкл,                 ├─► сирена через колонку Bluetooth (mpv радио)
                      удар / наклон / сдвиг)            ├─► камера: POST /protect-current (запись не удалится)
                                                        └─► outbox (SQLite) ─► fnav-relay POST /device/alarm
fnav-relay (сервер) ─ Web Push (VAPID, aes128gcm) ─► FCM / Mozilla / Apple ─► телефон (sw.js админки)
```

## 1. Подключение датчика (GY-521 / MPU-6050)

Своя шина **I2C-3** (`dtoverlay=i2c3,pins_4_5`, `/dev/i2c-3`): контакты 3/5 (I2C-1) заняты кабелем GPS — там
компас IST8310 (0x0E) и светодиод GPS (0x38). Датчик — адрес **0x68**.

| Вывод модуля | → | 40-pin | Сигнал Pi |
|---|---|---|---|
| VCC | → | **pin 17** | 3,3 В |
| GND | → | **pin 9** | GND |
| SDA | → | **pin 7** | GPIO4 / SDA3 |
| SCL | → | **pin 29** | GPIO5 / SCL3 |
| AD0, INT, XDA, XCL | — | — | не подключать (AD0 на модуле подтянут к GND → 0x68) |

fnav-core ищет датчик на `sensor_bus` (по умолчанию 3), не нашёл — на I2C-1 (можно подключить параллельно компасу
на контакты 3/5); найденная шина — в `AlarmStatus.sensor.bus`.

Провод до датчика — не длиннее 30–50 см. Крепление к металлу кузова жёсткое (винты/эпоксидка).

## 2. Датчик и обработка (fnav-core)

- Драйвер `fnav/sensors/mpu6050.py` (шина — как у компаса, `smbus2`, импорт лениво).
  WHO_AM_I: 0x68 — оригинал; клоны (0x70/0x71/0x72/0x73/0x98…) принимаются, значение пишется в лог и в статус.
  Настройка: гироскоп в standby, ±2 g, DLPF 94 Гц, частота 200 Гц, акселерометр → FIFO; FIFO читается ~10 раз
  в секунду кусками ≤ 32 байт; переполнение → сброс FIFO. Нет датчика / ошибка I2C → `sensor.ok=false`, повтор раз в 10 с.
- Чтение — в отдельном потоке; в event loop — только готовые величины.
- `g_lp` — сила тяжести (EMA, τ = 1 с). `d = |a − g_lp|` в g. `level_g` — максимум `d` за интервал телеметрии.
- **Удар**: `d ≥ impact_g` → событие `impact`; `peak_g` — максимум `d` за 1 с от первого превышения;
  следующий удар — не раньше чем через 2 с.
- **Наклон** (`tilt_enabled`): база — средний `g_lp` за 2 с в момент перехода в `armed`; `g_slow` — EMA τ = 5 с;
  угол между `g_slow` и базой ≥ `tilt_deg` дольше 5 с → событие `tilt`, база := текущий вектор.
- **Сдвиг по GPS** (`move_enabled`): точка отсчёта — первая точка с `fix` ≠ `none` и `hdop ≤ 5` после перехода в
  `armed`; расстояние ≥ `move_m` → событие `move` (`distance_m`, `lat`, `lon`); дальше — не чаще раза в 300 с и только
  если от последнего сообщённого места снова ≥ `move_m`.
- События и сирена — только в `armed`/`triggered`. Push одного вида (`kind`) — не чаще раза в `push_cooldown_s`
  (в журнал попадают все события).

### Состояния

`off` → (arm) → `arming` (`arm_delay_s`, обратный отсчёт `arm_in_s`) → `armed` → (событие) → `triggered`
(пока звучит сирена и ещё 30 с после последнего события) → `armed`. `disarm` из любого состояния → `off` и
сирена замолкает. Состояние охраны переживает перезапуск (`data_dir/alarm_state.json`); после перезапуска во
включённой охране — сразу `armed` (база наклона — через 2 с после старта).

### Действия при событии (кроме `test`)

1. Журнал (`data_dir/alarm_events.json`, последние 200), `Telemetry.alarm.seq += 1`.
2. Камера: `protect-current` (клиент fnav-core к fnav-camera) — сегмент не удалится автоматически;
   имя сегмента — в `data.segment`.
3. Сирена (`siren`): через mpv радио на колонку Bluetooth, если она подключена — текущий поток на паузу/замена,
   синтезированный в коде звук сирены (свой WAV в data_dir, без сторонних файлов), громкость 100, `siren_s` секунд,
   потом восстановить, что играло (станция, громкость) или остаться в тишине. Колонки нет — пропустить (в лог).
   **События камеры (`approach`, `face`, `loiter`) сирену не включают никогда** — [vision.md](vision.md).
4. Снимок камеры (`GET /snapshot.jpg` fnav-camera) → `data_dir/alarm_snaps/<id>.jpg` (последние 100), `data.snapshot =
   true`; удаляются вместе с журналом.
5. В outbox для сервера (если настроен `FNAV_RELAY_URL`/токен, как у журнала позиций).

### Аварийка (RC-реле на кнопку аварийной сигнализации)

Одно RC-реле (сигнал как у сервопривода, 50 Гц) параллельно кнопке аварийки с фиксацией: реле замкнуто — аварийка
мигает (мигает она сама), разомкнуто — нет. Других вариантов реле нет.

| Реле | → | 40-pin | Сигнал Pi |
|---|---|---|---|
| сигнал (S / PWM) | → | **pin 35** | GPIO19 — аппаратный PWM1 (`dtoverlay=pwm,pin=19,func=2`) |
| − | → | **pin 39** | GND |
| + | → | **pin 4** | 5 В |

`settings.alarm.hazard` (`HazardSettings`):

| Поле | По умолчанию | Пределы | Смысл |
|---|---|---|---|
| `enabled` | false | | мигать аварийкой при тревоге |
| `on_us` / `off_us` | 2000 / 1000 | 500–2500 | ширина импульса «вкл» / «выкл», мкс |
| `seconds` | 30 | 5–900 | сколько мигать после события (новое событие продлевает) |
| `confirm` | false | | мигнуть 1 раз при Arm и 2 раза при Disarm |

- Импульсы делает блок ШИМ (sysfs `/sys/class/pwm/pwmchip0/pwm1`, права — группе gpio): ровные при любой загрузке CPU
  (программный ШИМ при загруженном CPU теряет импульсы, и RC-реле «дребезжит»). Включена — импульсы «выкл» идут всё
  время; остановка fnav-core — «выкл», потом сигнал снимается. Нет `dtoverlay=pwm` — ошибка в `AlarmStatus.hazard.error`.
- Событие (не `test`) при `enabled` → реле замкнуто `seconds`; `disarm`/`silence` — разомкнуть сразу.
- `POST /api/alarm/hazard/test` — замкнуть на 3 с (даже при `enabled = false` и выключенной охране).
- `AlarmStatus.hazard = {enabled, active, error}`, `Telemetry.alarm.hazard: bool` (замкнуто сейчас).
- `--mock` — ненастоящий вывод (только состояние и лог).

## 3. Настройки `settings.alarm` (`AlarmSettings`)

| Поле | По умолчанию | Пределы | Смысл |
|---|---|---|---|
| `impact_g` | 0.15 | 0.02–2.0 | порог удара, g |
| `tilt_enabled` | true | | следить за наклоном |
| `tilt_deg` | 3.0 | 0.5–45 | порог наклона, ° |
| `move_enabled` | true | | следить за сдвигом по GPS |
| `move_m` | 100 | 20–5000 | порог сдвига, м |
| `siren` | true | | сирена через колонку |
| `siren_s` | 30 | 5–300 | длительность сирены, с |
| `arm_delay_s` | 60 | 0–600 | задержка постановки на охрану, с |
| `push_cooldown_s` | 60 | 0–3600 | push одного вида не чаще, с |
| `notify_offline` | false | | сервер шлёт push, если устройство на охране пропало из сети |
| `sensor_bus` | 1 | 0–30 | номер шины I2C |
| `sensor_address` | 104 (0x68) | 104 или 105 | адрес датчика |

## 4. API устройства (только с сессией, как остальной `/api`)

`AlarmEvent = {id: str (uuid4 hex), ts: float (unix), kind: "impact"|"tilt"|"move"|"approach"|"face"|"loiter"|"test", title: str, body: str,
url: str, data: object}` — `url` относительно каталога админки (`"#/alarm"`, для `move` — `"#/tracking"`).

Тексты: `impact` — «Impact detected» / «Hit 0.42 g at 14:03»; `tilt` — «Van tilted» / «Tilted 4.2° at 14:03»;
`move` — «Van is moving» / «230 m from the parking spot at 14:03»; `approach` — «Person approaching» / «Someone came
close to the van at 14:03»; `face` — «Someone is looking in» / «A face close to the windshield at 14:03»; `loiter` — «Someone is lingering at the van» /
«A person has stayed close to the van for 8 s at 14:03» (секунды — `data.held_s`); `test` — «Test alarm» / «Alarm notifications
work (14:03)». Время — локальное устройства, `HH:MM`.
События камер несут `data.camera` (`"front"` / `"rear"`). С задней — свои тексты: `approach` — «Person approaching behind
the van» / «Someone came close to the back of the van at 14:03»; `face` — «Someone is looking into the rear camera» /
«A face close to the rear camera at 14:03»; `loiter` — «Someone is lingering behind the van» / «A person has stayed close
to the back of the van for 8 s at 14:03». Пауза между push (`push_cooldown_s`) для событий камер — отдельно по камерам.
При любом срабатывании защищаются и снимаются обе камеры (задняя — если включена): `data.segment`, `data.snapshot`,
`data.segment_rear`, `data.snapshot_rear`; снимок задней — `GET /api/alarm/snapshot/{id}-rear.jpg`.

`AlarmStatus = {state, armed_at: float|null, arm_in_s: int|null, sensor: {ok: bool, model: str|null, address: int,
error: str|null}, level_g: float|null, tilt_deg: float|null (текущий угол от базы, null вне armed), siren: bool
(звучит), events: AlarmEvent[] (новые первыми, ≤ 100), relay: {configured: bool, pending: int}, hazard: {…},
vision: {enabled, available, watching, people, error, rear: {watching, people, error} | null} ([vision.md](vision.md))}`

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/api/alarm` | `AlarmStatus` |
| POST | `/api/alarm/arm` | `AlarmStatus` (`arming`; при `arm_delay_s = 0` — сразу `armed`) |
| POST | `/api/alarm/disarm` | `AlarmStatus` |
| POST | `/api/alarm/silence` | `AlarmStatus` (сирена замолкает, охрана остаётся) |
| POST | `/api/alarm/test` | `AlarmStatus` — событие `test` идёт всем путём (журнал → сервер → push), без сирены |
| GET | `/api/alarm/snapshot/{id}.jpg` | снимок камеры к событию журнала; 404 — нет |
| DELETE | `/api/alarm/events` | `AlarmStatus` (и снимки) |
| POST | `/api/alarm/simulate` | только в `--mock`: `{"kind": "impact"|"tilt"|"move"|"approach"|"face"|"loiter", "g": 0.5, "camera": "front"|"rear"}` → как настоящее событие (если `armed`; камера — с учётом `settings.alarm.vision`) |

Настройки — общим механизмом `settings` (как остальные разделы).

**Телеметрия** (WebSocket): `Telemetry.alarm = {state, arm_in_s: int|null, level_g: float|null, sensor_ok: bool,
siren: bool, seq: int, last: {kind, ts, title}|null}` (`null`, если сервиса нет).

**Кнопки**: действие `"alarm"` («Alarm on/off») — переключает `off` ↔ `arming`/`armed` (из `triggered` — `off`).

**Экран**: охрана не `off` — маленький значок в углу (`ARM 45` при обратном отсчёте, `ARMED`); `triggered` — крупная
мигающая надпись по `last.kind` (`IMPACT` / `TILTED` / `MOVING` / `PERSON` / `FACE`) 10 с после нового `seq`; переключение кнопкой —
вспышка `ALARM ON` / `ALARM OFF` (как вспышка режима радио).

## 5. Устройство → сервер

`POST {FNAV_RELAY_URL}/device/alarm`, заголовок `X-Fnav-Device-Token`, тело:
`{"state": {"armed": bool, "notify_offline": bool}, "events": [AlarmEvent, …]}` → `{"ok": true, "accepted": n}`.
Устройство шлёт: при изменении состояния, сразу при новых событиях и раз в 60 с (пульс). События лежат в outbox
(SQLite `data_dir/alarm_outbox.db`, переживает обрывы и перезагрузки) до ответа 200. Сервер не дублирует события
с уже известным `id`.

## 6. Сервер (fnav-relay)

- Web Push сам (RFC 8291 `aes128gcm` + VAPID RFC 8292, ES256) на `cryptography` (на сервере — apt `python3-cryptography`;
  импорт лениво, без неё push выключен с понятной ошибкой), запросы — `urllib`. Ключи VAPID создаются при первом
  запуске и хранятся в базе relay. `sub` в JWT: `FNAV_RELAY_VAPID_SUB`, иначе `https://<Host>` первого
  `/api/push/subscribe` (сохраняется). `exp` ≤ 12 ч.
- Заголовки: `TTL: 86400`, `Urgency: high` (`test` — `normal`), `Topic: <kind>`. Ответ 404/410 → подписка удаляется;
  прочие ошибки — `last_error` у подписки.
- Тело push (JSON): `{"title", "body", "tag": kind, "url", "ts"}`.
- Новые события из `/device/alarm` рассылаются в фоне (ответ устройству не ждёт рассылки).
- `notify_offline`: устройство было `armed` и молчит ≥ 180 с → один push «Van went offline» / «No contact for 3 min
  while the alarm is armed» (`url` `"#/tracking"`); вернулось → «Van is back online».

Для браузера (cookie сессии, как `/api/tracking`; nginx: `location ^~ /fnadmin/api/push/` → relay):

| Метод | Путь | Тело / ответ |
|---|---|---|
| GET | `/api/push/key` | `{"public_key": "<base64url, 65 байт P-256>"}` |
| POST | `/api/push/subscribe` | `{"subscription": {endpoint, keys: {p256dh, auth}}, "name": str}` → `{"ok": true, "id": n}` (тот же endpoint — обновить) |
| POST | `/api/push/unsubscribe` | `{"endpoint": str}` → `{"ok": true}` |
| GET | `/api/push/subscriptions` | `{"subscriptions": [{id, name, created, last_ok, last_error, host}]}` (`host` — хост endpoint'а) |
| DELETE | `/api/push/subscriptions/<id>` | `{"ok": true}` |
| POST | `/api/push/test` | `{"endpoint": str|null}` (null — всем) → `{"ok": true, "sent": n, "failed": n, "errors": [str]}` (синхронно) |

На устройстве (`http://…` без HTTPS) push недоступен: браузеры разрешают service worker и Push API только в
защищённом контексте. Устройство `/api/push/*` не обслуживает.

## 7. Веб (админка)

- `web/admin/sw.js` (область — каталог админки): `push` → `showNotification(title, {body, tag, data: {url},
  icon, badge, renotify: true})`; `notificationclick` → открыть/сфокусировать админку на `new URL(url,
  registration.scope)`. Обработчика `fetch` нет (страницы работают как без него). Регистрируется из админки только
  при `isSecureContext`.
- `web/admin/manifest.json` (`display: "standalone"`, `start_url: "./"`, `scope: "./"`, иконки PNG 192/512) +
  в `index.html` и `login.html`: `<link rel="manifest">`, `apple-touch-icon` (180), `apple-mobile-web-app-capable`,
  `apple-mobile-web-app-title`, `theme-color`.
- **iPhone** (iOS 16.4+): push только у веб-приложения на экране «Домой». В Safari не из «Домой» — инструкция:
  Share → Add to Home Screen → открыть оттуда → Enable notifications. Запрос разрешения — только по нажатию кнопки.
  Cookie у приложения с «Домой» свои — вход заново.
- **Android** (Chrome): подписка прямо в браузере; приложение тоже можно установить.
- Страница `#/alarm` («Alarm»): состояние и Arm/Disarm (обратный отсчёт), Silence; живой уровень вибрации с отметкой
  порога и пиком (для подбора `impact_g` стуком по кузову), текущий угол наклона; настройки `settings.alarm`;
  уведомления: включить на этом устройстве / тест / выключить, список подписанных устройств; журнал событий,
  «Send test alarm» (`/api/alarm/test`), Clear.
