# Контракт: демо-режим экрана и страница проекта (ptrs.ltd/apps/fallout.nav)

## 1. Демо-режим экрана — `web/display/index.html?demo=1` (агент B)

Экран работает **полностью в браузере**, без fnav-core и без устройства: для публичной страницы проекта.

- **Телеметрия** генерируется в JS (порт `fnav/mock/sensors.py`): маршрут по **Лас-Вегасу** (отсылка к Fallout: New Vegas) —
  Las Vegas Blvd (Strip) с поворотами на поперечные улицы, реалистичный профиль скорости со светофорами/остановками
  (на остановках < 2 м/с экран уходит в режим камеры), курс по маршруту, шум компаса, фикс 3D, 12 спутников, REC, internet=true.
- **Логика режима и зума** — порт `fnav/core/navlogic.py` в JS (таблица `nav.zoom_table`, гистерезис 2.0/2.5 м/с, выдержки).
- **Тайлы** — напрямую с Esri (`server.arcgisonline.com/...`, URL-шаблоны из `TileSettings`), не через `/tiles/`.
- **Камера** — статичная картинка: `?camera=<url>` (по умолчанию `camera-demo.jpg` рядом со страницей, см. страницу проекта).
- **Без PAL-сжатия** (`pal` не задан): холст 768×576 вписан и центрирован в окне, как сейчас без параметров. Зеркала нет.
- Работает с **любого подпути** (только относительные URL), без запросов к `/api`, `/ws`, `/tiles`.

### postMessage API

Страница → iframe: `{type: "fnav-demo", ...}` (любое подмножество полей):

| Поле | Значения | Действие |
|---|---|---|
| `play` | `true`/`false` | пауза симуляции (экран остаётся «живым», машина стоит) |
| `speedOverride` | число м/с или `null` | держать заданную скорость (ползунок на странице); `null` — вернуть профиль маршрута |
| `orientation` | `"heading_up"` / `"north_up"` | ориентация карты |
| `compassStyle` | `"v1"` / `"v2"` / `"v3"` | стиль компаса |
| `forceMode` | `"auto"` / `"map"` / `"camera"` | принудительный режим экрана |
| `layer` | `"imagery"` / `"street"` | спутник или карта улиц |

iframe → страница (~4 Гц): `{type: "fnav-demo-state", speedMps, headingDeg, mode, zoom, lat, lon, orientation, compassStyle}`.
iframe → страница один раз при готовности: `{type: "fnav-demo-ready"}`.

## 2. Страница проекта — `site/` (агент S)

- `site/index.html` + `site/assets/**` (+ css/js при необходимости). Язык — **английский**.
- Демо экрана встраивается как `<iframe src="display/index.html?demo=1&camera=../assets/camera-demo.jpg">`;
  папку `site/display/` собирает скрипт `site/build.sh` (копия `web/display/` без `dev/`).
- Фото: `site/assets/monitor-front.jpg` (RV-50 анфас, 1280×1077), `site/assets/cockpit.jpg` (кабина, 1280×960),
  `site/assets/camera-demo.jpg` (кадр «передней камеры», 768×576).
- Публикация: `https://ptrs.ltd/apps/fallout.nav/` (статика, nginx), исходники: `https://github.com/ceoptrsltd/fallout.nav`.
