# Контракт: охрана (датчик удара MPU-6050, наклон, сдвиг по GPS, пропажа видео камер) и push-уведомления

Тексты интерфейса, уведомлений и ответов API — на английском. Комментарии в коде — как в остальном проекте.

## Схема

```
MPU-6050 ─I2C-1─► fnav-core: AlarmService ──► событие ─┬─► журнал (data_dir/alarm_events.json), Telemetry.alarm
GPS (gpsd) ──────►   (охрана вкл/выкл,                 ├─► сирена через колонку Bluetooth (mpv радио)
камеры (/status) ─►   удар / наклон / сдвиг /           ├─► камера: POST /protect-current (запись не удалится)
                      пропало видео)                    │
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
- **Сдвиг по GPS** (`move_enabled`, `MoveWatch`, проверка раз в секунду): годятся только точки `fix = "3d"`
  (2D между домами и в паркинге скачет сильнее всего), `hdop ≤ 5`, не старше 5 с. Точка отсчёта — **медиана первых 5**
  годных точек после перехода в `armed` (выброс среди них, даже первой точкой, её не сдвигает). Событие `move`
  (`distance_m` — от точки отсчёта, `lat`, `lon`) — только когда годные точки дальше `move_m` идут **подряд: не меньше 5
  и от первой до последней ≥ 10 с**; точка ближе — счёт заново, годных точек не было дольше 30 с — тоже (редкие выбросы
  в паркинге не копятся). Одиночный скачок на 100–200 м (двор между домами, многоэтажный паркинг) сирену не включает.
  Дальше — не чаще раза в 300 с и только если от последнего сообщённого места снова ≥ `move_m` (подряд, по тому же
  правилу). Точка отсчёта и последнее сообщённое место переживают перезапуск (`alarm_state.json`), счёт «подряд» — нет:
  после перезапуска подтверждение то же.
- **Камера пропала** (`video_loss`) — раздел ниже.
- События и сирена — только в `armed`/`triggered`. Push одного вида (`kind`) — не чаще раза в `push_cooldown_s`
  (в журнал попадают все события; `armed`/`disarmed` и `test` — без паузы).

### Поставили / сняли с охраны (`armed`, `disarmed`)

Каждая постановка (`off` → `arming`/`armed`) и каждое снятие (не `off` → `off`) — запись журнала: `armed`
(`data.source`, `data.delay_s` — `arm_delay_s`, 0 — сразу) и `disarmed` (`data.source`, `data.was` — из какого
состояния: `arming`/`armed`/`triggered`). `data.source`: `"web"` — `POST /api/alarm/arm|disarm` (админка, на устройстве
или удалённо), `"keypad"` — клавиша «Alarm on/off». Повторное «включить» включённой (и «выключить» выключенной) — без
записи.

- **Push**: с клавиатуры — всегда (и постановка, и снятие); снятие посреди тревоги (`was = "triggered"`) — всегда, откуда
  бы ни сняли (хозяин должен узнать, что сирену и аварийку заглушили в кабине); постановка и снятие из админки вне
  тревоги — без push (хозяин сам). Пауза `push_cooldown_s` к ним не применяется: «сняли» вслед за «поставили» не теряется.
- **Не тревога**: без сирены, аварийки (кроме подтверждения `hazard.confirm`), защиты записи и снимков; состояние меняется
  только самим arm/disarm. Метками на записях не становятся. Экран плашку не показывает (там вспышка `ALARM ON` /
  `ALARM OFF`).

### Камера пропала (видео перестало идти)

Передняя камера (fnav-camera) и задняя (fnav-camera-rear, если `rear_camera.enabled`) сообщают в `/status`
`video_age_s` — сколько секунд нет кадров (`null` — после перезапуска конвейера кадров не было). Ядро опрашивает их
раз в секунду (`fnav/core/camera.py`); **видео идёт** — сервис отвечает, статус свежий (≤ 5 с), кадр не старше 3 с.
Не отвечает сам сервис камеры — тоже «нет видео». Кадр — с любого потока: задняя без основного потока (запись стоит),
но со вторым — картинка есть (снимки, fnav-watch), это не «камера пропала»; причина — в `error` её статуса.

`VideoWatch` (`fnav/core/alarm.py`), раз в секунду в `armed`/`triggered`, если `video_loss`:

- Следим только за камерой, у которой **после постановки на охрану видео было**. Не было (сломалась заранее, задняя
  выключена в сети) — не тревога: на странице охраны строка «Cameras» красная. После перезапуска ядра камера тоже
  должна сначала «показаться».
- Кадров нет дольше `video_loss_s` (по умолчанию 20 с) → событие **`video_lost`**: `data.camera` — `"front"` /
  `"rear"`, нет видео ни с одной — `"both"` (обе в одну проверку или вторая вслед за первой); `data.since` — когда
  пропало (unix). Одна пропажа — одно событие.
- Действия — как у событий камер ([vision.md](vision.md)): журнал, push, аварийка, защита записей и снимки обеих камер;
  **без сирены** (сбой Wi-Fi или перезагрузка камеры выглядят так же). У пропавшей камеры защищается последний
  сегмент (`/protect-current?recent=1`: сервис перезапустил конвейер — тот, что закрыт при остановке, если моложе
  10 мин), снимок — её кадр за 5 с до `data.since` (`/snapshot.jpg?before=<сейчас − since + 5>` из истории хаба).
  Метка на шкале записи и Play в журнале — с `data.since`.
- Видео снова идёт **10 с подряд** (`VIDEO_BACK_S`; мигающая камера — одна пропажа) → событие **`video_back`**
  (`data.camera`, `data.lost_s` — сколько не было): журнал и push, **не тревога** (состояние, сирена, аварийка не
  меняются; экран плашку не показывает).
- Короткие пропуски (сбой Wi-Fi, перезапуск сервиса камеры: задняя переподключается за 5–10 с, передняя — 2–4 с)
  короче `video_loss_s`. Камеру выключили в настройках (задняя) или `video_loss = false` — забыта, без `video_back`.
- Пропало всё устройство (питание, глушилка) — это видит только сервер: `notify_offline` (раздел 6).

### Состояния

`off` → (arm) → `arming` (`arm_delay_s`, обратный отсчёт `arm_in_s`) → `armed` → (событие) → `triggered`
(пока звучит сирена и ещё 30 с после последнего события) → `armed`. `disarm` из любого состояния → `off` и
сирена замолкает. Состояние охраны переживает перезапуск (`data_dir/alarm_state.json`); после перезапуска во
включённой охране — сразу `armed` (база наклона — через 2 с после старта). Перезапуск посреди отсчёта (`arming`) отсчёт
продолжает: в файле — срок по часам (`arm_until`, unix); остаток — не дольше `arm_delay_s` (часы после перезагрузки без
RTC могут отставать), срок прошёл, пока ядро стояло, — сразу `armed`. Иначе база наклона бралась бы, пока ещё
закрывают двери (удар / наклон → сирена).

### Сирена по кнопке (`siren`)

`POST /api/alarm/siren` (кнопка «Sound siren» на странице охраны, с подтверждением) — сирена на `siren_s` секунд
**в любом состоянии охраны** и даже при `siren = false` (паника: на снимке события камеры — человек у двери, а события
камер сирену не включают). Не тревога: состояние, аварийка, защита записей и снимки — как были. В журнал — `siren`
(`data.source` — `"web"`, `data.seconds`), push — только не из админки (как у `armed`/`disarmed`). Уже звучит — ничего.
Колонки нет — 503 `{"detail": "Siren: no Bluetooth speaker is connected"}`. Глушат её Stop (`silence`) и Disarm (и при
выключенной охране: тогда на странице охраны и на главной рядом с Arm — кнопка Stop).

### Действия при событии (кроме `test`, `armed`, `disarmed`)

Снимки и защита записи — **только у камер, у которых видео идёт сейчас** (`VideoWatch`: `get_video`). Задняя
выключена — её не спрашивают; камера есть, но кадров нет (выключена из сети, не подключена, сервис не работает) — ни
её снимка, ни записи (`data.snapshot*` / `data.segment*` нет), а не стоп-кадр или старая запись. Исключение —
пропавшая камера в событии `video_lost` (раздел «Камера пропала»). Сервис камеры и сам не отдаёт старых кадров
([camera.md](camera.md), `/snapshot.jpg`).

**Порядок**: сначала то, что от карты памяти не зависит — состояние `triggered` и журнал в памяти, сирена (п. 3),
аварийка, событие в outbox (п. 5, push); потом записи на карту — `alarm_state.json`, `alarm_events.json` (п. 1), защита
записи (п. 2) и снимки (п. 4). Каждый шаг сам по себе: карта полна или перемонтирована «только чтение» — сирена,
аварийка и push всё равно; ошибка записи — в лог и в `AlarmStatus.storage_error` (охрана работает, журнал и состояние —
в памяти до следующей удачной записи; после перезапуска несохранённое потеряно).

1. Журнал (`data_dir/alarm_events.json`, последние 200), `Telemetry.alarm.seq += 1`.
2. Камера: `protect-current` (клиент fnav-core к fnav-camera) — сегмент не удалится автоматически;
   имя сегмента — в `data.segment`.
3. Сирена (`siren`): через mpv радио на колонку Bluetooth, если она подключена — текущий поток на паузу/замена,
   синтезированный в коде звук сирены (свой WAV в data_dir, без сторонних файлов), громкость 100, `siren_s` секунд,
   потом восстановить, что играло (станция, громкость) или остаться в тишине. Колонки нет — пропустить (в лог).
   **События камеры (`approach`, `face`, `loiter`) и пропажа видео (`video_lost`) сирену не включают никогда** —
   [vision.md](vision.md).
4. Снимок камеры — кадр **за 5 с до события** (`SNAPSHOT_BEFORE_S`; `GET /snapshot.jpg?before=5` fnav-camera, кадр из
   её истории: видно, что к событию привело; кадров тогда не было — снимка нет) → `data_dir/alarm_snaps/<id>.jpg` (последние 100),
   `data.snapshot = true`; удаляются вместе с журналом. Запись события в админке (кнопка Play) тоже начинается за 5 с.
5. В outbox для сервера (если настроен `FNAV_RELAY_URL`/токен, как у журнала позиций) — с миниатюрой кадра (поле
   `thumb`, base64 JPEG ~5 КБ, как миниатюра журнала; камеры, поднявшей событие, иначе передней): кадры и защита записи
   запрашиваются сразу, push ждёт кадр не дольше 3 с (`PUSH_THUMB_WAIT_S`; не успел — push без картинки, снимок всё
   равно сохранится; сирена и аварийка не ждут). Та же миниатюра ложится в `alarm_snaps/thumbs/`. База outbox не пишется — событие
   ждёт отправки в памяти (раздел 5).

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
  время; остановка fnav-core — «выкл», потом сигнал снимается. Нет `dtoverlay=pwm` — ошибка в `AlarmStatus.hazard.error`
  (только у включённой: выключенную ШИМ не трогает — `disarm`/`silence` разомкнутое не «размыкают», вывод не открывают).
  Удачное открытие или переключение ошибку стирает. Открытие вывода (ожидание прав от udev) и закрытие (импульсы «выкл»
  ~0,1 с перед снятием сигнала) — в потоке, не в event loop.
- Событие (не `test`) при `enabled` → реле замкнуто `seconds`; `disarm`/`silence` — разомкнуть сразу.
- `POST /api/alarm/hazard/test` — замкнуть на 3 с (даже при `enabled = false` и выключенной охране). Выключенная не
  открылась — причина только в ответе (503), в статусе ошибки нет: ею не пользуются.
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
| `video_loss` | true | | тревога, если видео камеры на охране перестало идти |
| `video_loss_s` | 20 | 5–600 | столько без кадров — камера пропала, с |
| `sensor_bus` | 1 | 0–30 | номер шины I2C |
| `sensor_address` | 104 (0x68) | 104 или 105 | адрес датчика |

## 4. API устройства (только с сессией, как остальной `/api`)

`AlarmEvent = {id: str (uuid4 hex), ts: float (unix), kind: "impact"|"tilt"|"move"|"approach"|"face"|"loiter"|"video_lost"|"video_back"|"test"|"armed"|"disarmed"|"siren", title: str, body: str,
url: str, data: object}` — `url` относительно каталога админки (`"#/alarm"`, для `move` — `"#/tracking"`).

Тексты: `impact` — «Impact detected» / «Hit 0.42 g at 14:03»; `tilt` — «Van tilted» / «Tilted 4.2° at 14:03»;
`move` — «Van is moving» / «230 m from the parking spot at 14:03»; `approach` — «Person approaching» / «Someone came
close to the van at 14:03»; `face` — «Someone is looking in» / «A face close to the windshield at 14:03»; `loiter` — «Someone is lingering at the van» /
«A person has stayed close to the van for 8 s at 14:03» (секунды — `data.held_s`); `video_lost` — «Rear camera lost» /
«No video from the rear camera since 14:02» (`Front camera lost` …; обе — «Both cameras lost» / «No video from either
camera since 14:02»; время — `data.since`); `video_back` — «Rear camera is back» / «Video from the rear camera again at
14:05, after 3 min without it»; `test` — «Test alarm» / «Alarm notifications work (14:03)»; `armed` — «Alarm armed» /
«Armed with the keypad at 14:03, active in 60 s» (из админки — «from the app»; `delay_s = 0` — без «active in»);
`disarmed` — «Alarm disarmed» / «Disarmed from the app at 14:05» (посреди тревоги — «… while the alarm was going off»);
`siren` — «Siren sounded» / «Sounded from the app at 14:05 for 30 s».
Время — локальное устройства, `HH:MM`.
События камер несут `data.camera` (`"front"` / `"rear"`). С задней — свои тексты: `approach` — «Person approaching behind
the van» / «Someone came close to the back of the van at 14:03»; `face` — «Someone is looking into the rear camera» /
«A face close to the rear camera at 14:03»; `loiter` — «Someone is lingering behind the van» / «A person has stayed close
to the back of the van for 8 s at 14:03». Пауза между push (`push_cooldown_s`) для событий камер (и `video_lost` /
`video_back`) — отдельно по камерам (`data.camera`).
При любом срабатывании защищаются и снимаются обе камеры (задняя — если включена): `data.segment`, `data.snapshot`,
`data.segment_rear`, `data.snapshot_rear`; снимок задней — `GET /api/alarm/snapshot/{id}-rear.jpg`.

`AlarmStatus = {state, armed_at: float|null, arm_in_s: int|null, sensor: {ok: bool, model: str|null, address: int,
error: str|null}, level_g: float|null, tilt_deg: float|null (текущий угол от базы, null вне armed), siren: bool
(звучит), events: AlarmEvent[] (новые первыми, ≤ 100), relay: {configured: bool, pending: int}, hazard: {…},
vision: {enabled, available, watching, people, error, rear: {watching, people, error} | null, front: {…} | null — ошибка передней отдельно: её нет, а задняя смотрится — предупреждение, не отказ} ([vision.md](vision.md)),
cameras: [{camera: "front"|"rear", video: bool (кадры идут), lost: bool (было video_lost, ещё не вернулась)}] (задняя —
если включена), storage_error: str|null (что не пишется на карту: «Alarm state and log are not saved: …» — файлы
охраны, «Notifications wait in memory only (lost on restart): …» — outbox, «Notifications wait in memory and in
alarm_outbox.db.pending.json: …» — outbox, но очередь лежит в запасном файле; охрана при этом работает; на странице
охраны — красная строка «Storage»), seq: int (как `Telemetry.alarm.seq`), events_rev: int (номер правки журнала: новое
событие, снимок / запись к нему, очистка; после перезапуска — другой)}`

| Метод | Путь | Ответ |
|---|---|---|
| GET | `/api/alarm` | `AlarmStatus`; `?events=N` — только N последних событий (главная), `?events=0` — без журнала (опрос страницы охраны раз в 1,5 с: ~1 КБ вместо ~35 КБ) |
| GET | `/api/alarm/events?limit=100` | `{"rev": events_rev, "seq", "events": AlarmEvent[]}` (новые первыми, ≤ 200) — страница охраны берёт журнал, только когда в опросе сменился `events_rev` (новый `seq` в телеметрии — опрос сразу); у старого устройства без `events_rev` — полный `GET /api/alarm`, как раньше |
| POST | `/api/alarm/arm` | `AlarmStatus` (`arming`; при `arm_delay_s = 0` — сразу `armed`); в журнал — `armed`, `source: "web"` |
| POST | `/api/alarm/disarm` | `AlarmStatus`; в журнал — `disarmed`, `source: "web"` (push — только если снята посреди тревоги) |
| POST | `/api/alarm/silence` | `AlarmStatus` (сирена замолкает, охрана остаётся) |
| POST | `/api/alarm/siren` | `AlarmStatus` — сирена по кнопке (раздел «Сирена по кнопке»); не зазвучала — 503 `{"detail": "Siren: <почему>"}` |
| POST | `/api/alarm/test` | `AlarmStatus` — событие `test` идёт всем путём (журнал → сервер → push), без сирены |
| GET | `/api/alarm/snapshot/{id}.jpg` | снимок камеры к событию журнала; 404 — нет. `?thumb=1` — миниатюра для списков (192×144, ~5 КБ; уменьшение при декодировании JPEG): делается при первом запросе, лежит в `alarm_snaps/thumbs/`, удаляется вместе со снимком и при очистке журнала |
| DELETE | `/api/alarm/events` | `AlarmStatus` (и снимки) |
| POST | `/api/alarm/simulate` | только в `--mock`: `{"kind": "impact"|"tilt"|"move"|"approach"|"face"|"loiter", "g": 0.5, "camera": "front"|"rear"}` → как настоящее событие (если `armed`; камера — с учётом `settings.alarm.vision`); `{"kind": "video_lost", "camera", "seconds": 60}` — у mock-камеры пропадает видео на `seconds` (событие — обычным путём, через `video_loss_s`; камера не mock или выключена — 409) |

Настройки — общим механизмом `settings` (как остальные разделы).

**Телеметрия** (WebSocket): `Telemetry.alarm = {state, arm_in_s: int|null, level_g: float|null, sensor_ok: bool,
siren: bool, seq: int, last: {kind, ts, title}|null}` (`null`, если сервиса нет).

**Кнопки**: действие `"alarm"` («Alarm on/off») — переключает `off` ↔ `arming`/`armed` (из `triggered` — `off`); в
журнал — `armed`/`disarmed` с `source: "keypad"` и push хозяину (раздел «Поставили / сняли с охраны»).

**Экран**: охрана не `off` — маленький значок в углу (`ARM 45` при обратном отсчёте, `ARMED`); `triggered` — крупная
мигающая надпись по `last.kind` (`IMPACT` / `TILTED` / `MOVING` / `PERSON` / `FACE` / `CAMERA`) 10 с после нового `seq`
(`video_back`, `test`, `armed`, `disarmed`, `siren` — без надписи); переключение кнопкой —
вспышка `ALARM ON` / `ALARM OFF` (как вспышка режима радио).

## 5. Устройство → сервер

`POST {FNAV_RELAY_URL}/device/alarm`, заголовок `X-Fnav-Device-Token`, тело:
`{"state": {"armed": bool, "notify_offline": bool}, "events": [AlarmEvent, …]}` → `{"ok": true, "accepted": n}`.
У события может быть `thumb` — миниатюра кадра (base64 JPEG, ~5 КБ; раздел «Действия при событии», п. 5); в журнал
устройства оно не попадает, сервер без поддержки снимков его просто пропускает.
Устройство шлёт: при изменении состояния, сразу при новых событиях и раз в 60 с (пульс). События лежат в outbox
(SQLite `data_dir/alarm_outbox.db`, переживает обрывы и перезагрузки) до ответа 200. Сервер не дублирует события
с уже известным `id` (повтор после сбоя outbox не страшен).

Outbox не должен выключать охрану (`AlarmOutbox`, `fnav/core/alarm.py`):

- При старте файл испорчен (пропало питание: `file is not a database`, `malformed`) — он уходит в сторону
  (`alarm_outbox.db.bad-<ГГГГММДД-ЧЧММСС>`, с ним `-wal`/`-shm`), база создаётся заново (что не ушло на сервер — в
  убранном файле).
- Не открывается и так (карта «только чтение», полна, файл не убрать) или перестала писаться/читаться на ходу — события
  ждут отправки **в памяти** (до 1000; что ещё читается из базы — туда же), push уходит как обычно; причина — в
  `AlarmStatus.storage_error`. Базу пробуем открыть снова при отправке, не чаще раза в минуту (`OUTBOX_RETRY_S`; битая —
  в сторону, как при старте): открылась — очередь из памяти уходит в неё, `storage_error` пропадает.
- Пока база не работает, очередь из памяти лежит ещё и в маленьком запасном файле `alarm_outbox.db.pending.json` (без
  миниатюр), если его можно записать (не годится база — чужой владелец файла, сбой SQLite, — а файл рядом пишется):
  после перезапуска она снова в очереди (база открылась — в базу, файл удаляется; нет — в память). Не пишется и он
  (карта «только чтение») — очередь только в памяти, до перезапуска.
- Запуск охраны в ядре не зависит от отметок, fnav-watch, уровня и датчика (и наоборот): каждый запускается сам по себе,
  сбой одного — в лог.

## 6. Сервер (fnav-relay)

- Web Push сам (RFC 8291 `aes128gcm` + VAPID RFC 8292, ES256) на `cryptography` (на сервере — apt `python3-cryptography`;
  импорт лениво, без неё push выключен с понятной ошибкой), запросы — `urllib`. Ключи VAPID создаются при первом
  запуске и хранятся в базе relay. `sub` в JWT: `FNAV_RELAY_VAPID_SUB`, иначе `https://<Host>` первого
  `/api/push/subscribe` (сохраняется). `exp` ≤ 12 ч.
- Заголовки: `TTL: 86400`, `Urgency: high` (`test` — `normal`), `Topic: <kind>`. Ответ 404/410 → подписка удаляется;
  прочие ошибки — `last_error` у подписки.
- Тело push (JSON): `{"title", "body", "tag": kind, "url", "ts", "image"?}`. `image` — снимок к событию: пришёл с
  событием `thumb` (JPEG ≤ 64 КБ) — relay кладёт его файлом `push_snaps/<token>.jpg` рядом со своей базой (на сервере —
  `/var/lib/fnav-relay/push_snaps`), в push — только ссылка `../api/push/snap/<token>.jpg` (относительно каталога
  админки, как `url`; предел Web Push ~4 КБ — картинку в push не кладут). `token` — 24 случайных байта (base64url):
  ссылка не угадывается и открывается **без входа** (картинку уведомления браузер грузит сам, без cookie), 2 суток
  (`SNAP_TTL_S`, TTL push — сутки), хранится не больше 500 снимков; старые удаляются при записи нового.
- Новые события из `/device/alarm` рассылаются в фоне (ответ устройству не ждёт рассылки).
- `notify_offline`: устройство было `armed` и молчит ≥ 180 с → один push «Van went offline» / «No contact for 3 min
  while the alarm is armed» (`url` `"#/tracking"`); вернулось → «Van is back online».

Для браузера (cookie сессии, как `/api/tracking`; nginx: `location ^~ /fnadmin/api/push/` → relay):

| Метод | Путь | Тело / ответ |
|---|---|---|
| GET | `/api/push/key` | `{"public_key": "<base64url, 65 байт P-256>"}` |
| GET | `/api/push/snap/<token>.jpg` | снимок к push, **без входа**; `image/jpeg`, `Cache-Control: private, max-age=<сколько ещё живёт>`; нет / устарел — 404 |
| POST | `/api/push/subscribe` | `{"subscription": {endpoint, keys: {p256dh, auth}}, "name": str}` → `{"ok": true, "id": n}` (тот же endpoint — обновить) |
| POST | `/api/push/unsubscribe` | `{"endpoint": str}` → `{"ok": true}` |
| GET | `/api/push/subscriptions` | `{"subscriptions": [{id, name, created, last_ok, last_error, host}]}` (`host` — хост endpoint'а) |
| DELETE | `/api/push/subscriptions/<id>` | `{"ok": true}` |
| POST | `/api/push/test` | `{"endpoint": str|null}` (null — всем) → `{"ok": true, "sent": n, "failed": n, "errors": [str]}` (синхронно) |

На устройстве (`http://…` без HTTPS) push недоступен: браузеры разрешают service worker и Push API только в
защищённом контексте. Устройство `/api/push/*` не обслуживает.

## 7. Веб (админка)

- `web/admin/sw.js` (область — каталог админки): `push` → `showNotification(title, {body, tag, data: {url},
  icon, badge, renotify: true, image?, actions?})`; `notificationclick` → открыть/сфокусировать админку на `new URL(url,
  registration.scope)`. Обработчика `fetch` нет (страницы работают как без него). Регистрируется из админки только
  при `isSecureContext`.
  - `image` — `new URL(image, registration.scope)` (снимок к событию, раздел 6): Android показывает большую картинку.
  - Кнопка **Silence** (`actions: [{action: "silence"}]`) — у всего, что может включить сирену или аварийку; у заметок
    (`test`, `armed`, `disarmed`, `video_back`, `offline`, `siren`) её нет. Нажата — `POST new URL("../api/alarm/silence",
    scope)` с `credentials: "same-origin"` (cookie сессии; на устройстве это `/api/…`, удалённо — `/fnadmin/api/…`), окно
    не открывается; итог — уведомление с `tag` `silence`: «Siren and hazard lights stopped» (тихое) или «Could not
    silence the alarm» с причиной (нет связи, вышли из приложения — 401, фургон не в сети — 503 relay) — по нажатию
    открывает страницу охраны. iOS кнопок у уведомлений не показывает: там нажатие открывает страницу охраны, как раньше.
- `web/admin/manifest.json` (`display: "standalone"`, `start_url: "./"`, `scope: "./"`, иконки PNG 192/512) +
  в `index.html` и `login.html`: `<link rel="manifest">`, `apple-touch-icon` (180), `apple-mobile-web-app-capable`,
  `apple-mobile-web-app-title`, `theme-color`.
- **iPhone** (iOS 16.4+): push только у веб-приложения на экране «Домой». В Safari не из «Домой» — инструкция:
  Share → Add to Home Screen → открыть оттуда → Enable notifications. Запрос разрешения — только по нажатию кнопки.
  Cookie у приложения с «Домой» свои — вход заново.
- **Android** (Chrome): подписка прямо в браузере; приложение тоже можно установить.
- Страница `#/alarm` («Alarm»): состояние и Arm/Disarm (обратный отсчёт), Silence, «Sound siren» (с подтверждением;
  пока сирена звучит — скрыта); опрос `GET api/alarm?events=0` раз в 1,5 с, журнал — `GET api/alarm/events`, только
  когда сменился `events_rev` (раньше каждый опрос нёс 100 событий, ~35 КБ: ~85 МБ/ч через туннель); живой уровень вибрации с отметкой
  порога и пиком (для подбора `impact_g` стуком по кузову), текущий угол наклона; настройки `settings.alarm`;
  уведомления: включить на этом устройстве / тест / выключить, список подписанных устройств; журнал событий
  (`armed`/`disarmed` — «Armed»/«Disarmed» с замком, рядом откуда: `keypad` / `app`, `during the alarm`; `siren` —
  «Siren sounded», `app`),
  «Send test alarm» (`/api/alarm/test`), Clear; `storage_error` — красная строка «Storage» в состоянии.
- Кнопки Arm / Disarm / Stop (страница Alarm и Dashboard): после ответа на действие новые кнопки ~1,5 с неактивны
  и приглушены (`ACT_COOL_MS`, `actCooldown` в `web/admin/js/dash-fmt.js`). Disarm встаёт ровно на место Arm (Arm —
  на место Disarm, Disarm — на место Stop), а ответ по Wi-Fi фургона приходит за ~50 мс: двойное нажатие на Arm или
  второе «нажалось ли?» выключало охрану. Если набор кнопок меняется ещё раз в эти 1,5 с (страница Alarm следует
  телеметрии, и новое состояние приходит на пару сотен мс позже ответа), отсчёт начинается заново.
- Новая версия админки после деплоя (баннер с Reload, «Reload app» в листе More) — `docs/contracts/remote.md`, раздел 6.
