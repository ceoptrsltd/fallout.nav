# Отчёт этапа 0 — устройство (2026-10-20)

| Параметр | Значение |
|---|---|
| Модуль | Raspberry Pi Compute Module 4 Rev 1.1, 4 ГБ ОЗУ, 4 ядра |
| Хранилище | eMMC 29.1 ГБ: `/boot/firmware` 512 МБ, `/` 28.6 ГБ (занято 6.7 ГБ, свободно 21 ГБ) |
| ОС | Raspberry Pi OS **с рабочим столом** (lightdm + labwc), Debian 13 trixie, ядро 6.18.50+rpt-rpi-v8, Python 3.13.5 |
| Предустановлено | chromium 154, python3-picamera2 0.3.37, rpicam-apps 1.13, ffmpeg 7.1, i2c-tools, python3-lgpio, python3-gpiozero, python3-numpy |
| Сеть | Wi‑Fi `PhoneHotspot`, 172.23.204.16/24, рег. домен PL |
| SSH | пользователь `falloutnav`, вход по ключу настроен; sudo требует пароль |
| Температура / троттлинг | 38 °C / `0x0` |

## Изменения в /boot/firmware (резервные копии `*.orig-fnav`)
- `config.txt`: блок `# >>> fnav … # <<< fnav` — `enable_uart=1`, `dtoverlay=disable-bt`, `dtparam=i2c_arm=on`, `camera_auto_detect=0`, `dtoverlay=ov5647,cam0`.
- `cmdline.txt`: убрана serial-консоль `console=serial0,115200`.
- Отключены `hciuart`, serial-getty на ttyAMA0.

## Железо
- **Камера OV5647** (CSI0) — работает. Режимы сенсора:
  640×480 @ 62.5 (crop) · 1296×972 @ 46.3 (полный кадр, биннинг) · 1920×1080 @ 32.8 (**обрезка**) · **2592×1944 @ 15.63 (полный кадр)**.
  Тест: `rpicam-vid --mode 2592:1944:10:P --width 1440 --height 1080 --framerate 15 --codec h264` — аппаратный кодер принял 1440×1080, файл корректный.
- **UART** `/dev/ttyAMA0` (PL011 на GPIO14/15) — есть, данных нет → GPS не подключён на момент проверки.
- **I2C-1** — шина есть, устройств нет → компас не подключён на момент проверки.

## Бюджет хранилища
Свободно 21 ГБ − кэш карты 1 ГБ − резерв 2 ГБ − пакеты ~1 ГБ ≈ **17 ГБ под видео** → при ~2.7 ГБ/ч ≈ **6 часов** записи по кругу.
