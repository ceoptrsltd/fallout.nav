# Контракт: интернет-радио, Bluetooth-колонка, плашка радио на экране

Тексты интерфейса и ответов API — на английском. Комментарии в коде — как в остальном проекте (русский).

## Схема

```
админка (Radio, Bluetooth) ──REST──► fnav-core ──► radio-browser.info (каталог станций, JSON)
                                         │
                                         ├─ fnav/core/radio.py ──IPC (unix socket)──► fnav-radio.service (mpv --idle)
                                         │                                              └─► ALSA bluealsa ─► BT-колонка (A2DP)
                                         ├─ fnav/core/bluetooth.py ──D-Bus──► bluetoothd (BlueZ) + bluealsa
                                         └─ телеметрия /ws: radio {…} ──► экран (web/display): плашка радио
```

Встроенный Bluetooth CM4 работает на основном PL011 UART0 (после переноса GPS на UART3, GPIO4/5 — делает ведущий).
Звук — только через Bluetooth-колонку (аналогового выхода нет: `dtparam=audio=off`).

## 1. Каталог станций (как в myAIfriend, `RadioBrowser.kt` / `MusicGenres.kt`)

- Зеркало: случайное имя из `https://all.api.radio-browser.info/json/servers`, при ошибке — `all.api.radio-browser.info`;
  повтор один раз через 0,7 с с новым зеркалом. `User-Agent: fallout.nav/1.0 (car radio)`.
- Поиск: `/json/stations/search?<критерий>&hidebroken=true&order=clickcount&reverse=true&limit=N`
  - жанр: `tag=<tag>` (N=60); для речевых тегов (`news`, `talk`, `podcast`, `sport`, `comedy`) — ещё `&language=english`;
  - по имени: `name=<q>` (N=100);
  - популярное: `countrycode=XX` или `is_https=true` для мира (N=30).
- При запуске станции — `GET /json/url/<stationuuid>` (каталог считает прослушивания), ошибки игнорируются.
- Жанровые плитки (tag, emoji, label, kind) — ровно список `MusicGenres.TILES`, английские подписи:
  MOOD: relax 🌊 Calm, sleep 🌙 Sleep, lofi 📚 Study, workout 💪 Workout, party 🎉 Upbeat, romantic 💞 Romantic,
  meditation 🧘 Meditation; GENRE: pop 🎤 Pop, rock 🎸 Rock, jazz 🎷 Jazz, classical 🎻 Classical,
  electronic 🎛️ Electronic, hip hop 🎧 Hip-hop, chanson 🪗 Chanson, lounge 🍸 Lounge, dance 💃 Dance, blues 🎺 Blues,
  folk 🪕 Folk, kids 🧸 Kids; ERA: hits 🔥 Hits, oldies 📻 Oldies, 80s 📼 80s, 90s 💿 90s, news 📰 News.
- Станция (`Station`): `{"uuid","name","url" (url_resolved, иначе url),"homepage","favicon","tags","country",
  "countrycode","language","codec","bitrate","votes","clickcount","hls":bool}`.

## 2. API fnav-core

| Метод | Путь | Что |
|---|---|---|
| GET | `/api/radio/genres` | `{"genres":[{"tag","emoji","label","kind":"mood"\|"genre"\|"era"}]}` |
| GET | `/api/radio/stations?tag=…` \| `?q=…` \| `?top=world\|XX` | `{"stations":[Station…]}`; 503 `{"detail":"Radio directory is unreachable"}` |
| GET | `/api/radio/state` | `RadioState` (ниже) |
| POST | `/api/radio/play` | `{"station":Station,"genre":"jazz"?}` → `RadioState` |
| POST | `/api/radio/stop` | → `RadioState` |
| POST | `/api/radio/resume` | играть последнюю станцию → `RadioState` (409, если её нет) |
| POST | `/api/radio/volume` | `{"volume":0..100}` → `RadioState` (сохраняется в `radio.volume`) |
| GET | `/api/bluetooth/state` | `BtState` (ниже) |
| POST | `/api/bluetooth/power` | `{"on":bool}` |
| POST | `/api/bluetooth/scan` | `{"seconds":15}` — поиск устройств N секунд (1…60) |
| POST | `/api/bluetooth/devices/{mac}/connect` | если нужно — pair + trust, затем connect; аудиоустройство становится колонкой по умолчанию |
| POST | `/api/bluetooth/devices/{mac}/disconnect` | |
| DELETE | `/api/bluetooth/devices/{mac}` | забыть (RemoveDevice); если это колонка по умолчанию — сбросить |

`mac` — `AA:BB:CC:DD:EE:FF` (проверять формат). Долгие операции (pair/connect до ~30 с) — ответ после завершения;
ошибки BlueZ → 409/502 с понятным `detail` («Pairing failed: …», «Device not found» → 404).

```
RadioState = {
  "state": "stopped" | "connecting" | "playing" | "waiting_output" | "error",
  "station": Station | null,       # текущая (или последняя, если stopped)
  "genre": "jazz" | null,          # плитка, из которой выбрана станция
  "track": "Artist - Title" | null,# ICY StreamTitle / metadata mpv; null — поток не сообщает
  "volume": 60,
  "output": {"address","name","connected":bool} | null,   # колонка по умолчанию
  "error": str | null,
  "recent": [Station…]             # последние 12 запущенных, новые первыми
}
BtState = {
  "available": bool, "powered": bool, "discovering": bool,
  "audio_device": "AA:…" | null,   # колонка по умолчанию (автоподключение)
  "devices": [{"address","name","icon","paired","trusted","connected","rssi":int|null,"audio":bool}]
}
```

## 3. Поведение

- Воспроизведение: служба `fnav-radio.service` (пользователь fnav) — `mpv --idle=yes --no-video --no-terminal
  --input-ipc-server=/run/fnav-radio/mpv.sock …`, fnav-core управляет ею по JSON IPC (loadfile, stop, volume,
  observe_property `metadata`/`media-title`/`idle-active`/`core-idle`). Аудиоустройство mpv —
  `alsa/bluealsa:DEV=<mac>,PROFILE=a2dp` подключённой колонки (ставится при подключении).
- Нет подключённой колонки → `waiting_output` (поток не открываем — не тратим трафик); колонка подключилась → играем.
- Поток оборвался / нет интернета → `connecting`, повтор с паузой 2→4→…→30 с, пока станция выбрана.
- Состояние (последняя станция, «играло ли», recent) — в `data_dir/radio_state.json`; колонка по умолчанию — там же
  или в `data_dir/bluetooth_state.json`. После перезагрузки: если `radio.resume_on_boot` и радио играло — продолжить.
- Автоподключение: при старте и каждые 30 с, пока колонка по умолчанию не подключена и адаптер включён, — Connect.
- Трафик: только пока играет станция (128 кбит/с ≈ 60 МБ/ч).

## 4. Настройки (`fnav/models.py`)

```
RadioSettings:     volume: int 0..100 = 60;  resume_on_boot: bool = True
BluetoothSettings: enabled: bool = True (адаптер включать при старте);  auto_connect: bool = True
DisplaySettings:   radio_panel: bool = True  (плашка радио на экране)
```

## 5. Телеметрия → экран

`Telemetry.radio: RadioStatus | null` (каждый кадр, компактно):
`{"state","station":"name"|null,"track":str|null,"genre":"Jazz"|null (подпись плитки или первый тег станции),
"volume":int,"output":"JBL Go 3"|null,"output_connected":bool}`.

Экран (web/display) показывает плашку, когда `display.radio_panel` и `state` ∈ {playing, connecting, waiting_output}.
Плашка — в стиле HUD (как компас и спидометр: те же шрифты, рамки, монохром, читаемо на 5″ ч/б CRT 720×480 с полями),
название станции крупно, трек — бегущей строкой, если не помещается; смена трека — заметная, но короткая анимация;
`connecting` / `waiting_output` — свои понятные состояния. Не перекрывает компас, спидометр, индикаторы и машину.
Демо-режим (`?demo=1`) показывает плашку с вымышленной станцией и сменой треков.
