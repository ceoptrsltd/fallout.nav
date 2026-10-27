# Контракт: камера на охране — fnav-watch ↔ fnav-core

Пока охрана включена (`armed`/`triggered`) и `settings.alarm.vision.enabled`, отдельный процесс **fnav-watch** смотрит
в кадр и сообщает два события:

| kind | Когда | Текст push / журнала |
|---|---|---|
| `approach` | человек подходит: его рамка выросла, нижний край (ноги) дошёл до ближней линии `near_line` и держится там `approach_hold_s` | «Person approaching» |
| `face` | крупное лицо (ширина ≥ `face_min` ширины кадра) около центра кадра держится `face_hold_s` | «Someone is looking in» |

Реакция ядра — как на событие датчика, **но без сирены**: журнал, аварийка (если `alarm.hazard.enabled`),
защита текущей записи, снимок камеры к событию, push (не чаще `push_cooldown_s` на вид).

## Настройки `settings.alarm.vision` (fnav/models.py `VisionSettings`)

| Поле | По умолчанию | |
|---|---|---|
| `enabled` | false | распознавание вообще |
| `approach` | true | событие `approach` |
| `near_line` | 0.6 | ближняя линия: доля высоты кадра сверху (0.2…1), рисуется в админке на снимке — чуть выше капота |
| `approach_hold_s` | 2.0 | столько у линии — тревога (идущие мимо не успевают) |
| `face` | true | событие `face` |
| `face_min` | 0.08 | ширина лица — доля ширины кадра |
| `face_hold_s` | 3.0 | |

fnav-watch читает `fnav.paths.settings_file()` сам (как fnav-camera), файл не пишет; `POST /reload` — перечитать.

## HTTP fnav-watch: 127.0.0.1:8082 (только localhost, без авторизации)

| Метод | Путь | |
|---|---|---|
| GET | `/status` | `{"watching": bool, "people": int, "seq": int, "events": [{"seq": int, "kind": "approach"\|"face", "ts": unix, "data": {…}}], "error": str\|null, "fps": float\|null}` — `events`: последние ≤ 20, `seq` растёт с 1 от запуска процесса |
| POST | `/arm` | `{"watching": bool}` — смотреть / не смотреть (не смотрит — кадры не берёт, CPU ≈ 0) |
| POST | `/reload` | перечитать настройки |

`data` — что помогает разобраться потом (размер рамки, сколько держалось, уверенность); ядро кладёт его в журнал.

## Ядро (fnav/core/vision.py `VisionClient`)

- Раз в секунду `GET /status`, пока распознавание включено (или сервис мог остаться смотреть).
- Хочет `watching = enabled and alarm.state in (armed, triggered)`; не совпало с `/status` — `POST /arm`
  (так и после перезапуска fnav-watch).
- Новые события (`seq` больше последнего виденного; `seq` уменьшился — сервис перезапустился, счёт заново; старше
  30 с — не тревога) → `AlarmService.vision_event(kind, data)`: проверяет `enabled`, вид и состояние охраны.
- Статус в `AlarmStatus.vision`: `enabled`, `available`, `watching`, `people`, `error`.
- Снимок к любому событию журнала (кроме `test`): `GET /api/alarm/snapshot/{event_id}.jpg`, хранятся последние 100
  (`<data_dir>/alarm_snaps/`), удаляются вместе с журналом. `--mock`: `POST /api/alarm/simulate` с `approach`/`face`.

## Ресурсы (главное — не мешать записи и экрану)

- Отдельный процесс и systemd-сервис с низким приоритетом и ограничением CPU; падение fnav-watch не трогает
  fnav-camera и fnav-core.
- Кадры — с уже существующего MJPEG fnav-camera (`/mjpeg?fps=N`), конвейер камеры не меняется.
- Дешёвый детектор движения на маленьком кадре всё время, нейросеть — только когда есть движение.
- Детали (рантайм, модели, частоты) — по исследованию для CM4; дополняются здесь при реализации.
