"""Общие модели данных fallout.nav — ЕДИНЫЙ КОНТРАКТ между всеми частями.

Владелец: ведущий (интегратор). Субагенты этот файл НЕ правят — если нужно поле,
опишите его в отчёте, ведущий добавит.

Здесь:
  * Settings — все настройки (хранятся в {data_dir}/settings.json, правятся через веб-интерфейс);
  * SensorSnapshot и составные части — то, что отдаёт SensorHub;
  * CameraStatus — то, что отдаёт сервис камеры по HTTP;
  * Telemetry — сообщение WebSocket, которое fnav-core рассылает 10 раз в секунду.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


# ───────────────────────────── Настройки ─────────────────────────────


class ScreenSize(_Model):
    """Видимая область физического экрана RV-50, мм (4:3)."""

    w_mm: float = Field(92.0, gt=10, lt=500)
    h_mm: float = Field(69.0, gt=10, lt=500)


class MapLayers(_Model):
    imagery: bool = True  # Esri World_Imagery (спутник)
    transport: bool = True  # Esri Reference/World_Transportation (улицы поверх снимка)
    places: bool = True  # Esri Reference/World_Boundaries_and_Places (подписи)
    street_map_instead: bool = False  # вместо спутника — Esri World_Street_Map


class DisplaySettings(_Model):
    orientation: Literal["heading_up", "north_up"] = "heading_up"
    compass_style: str = "v1"  # id варианта дизайна компаса (web/display/ui/compass.js)
    screen: ScreenSize = ScreenSize()
    safe_area_pct: float = Field(5.0, ge=0, le=20)  # поля, которые обрезает кинескоп, % с каждой стороны
    speed_units: Literal["kmh", "mps"] = "kmh"
    # где стоит машина в режиме heading_up: доля высоты холста сверху (0.5 = центр, 0.67 = нижняя треть)
    vehicle_anchor_y: float = Field(0.67, ge=0.3, le=0.9)
    # heading_up: иконка машины поворачивается почти сразу, карта догоняет её с замедлением
    vehicle_rotation_lag_s: float = Field(0.25, ge=0, le=3)  # постоянная времени поворота иконки
    map_rotation_lag_s: float = Field(1.5, ge=0, le=10)  # постоянная времени «догоняния» карты
    map_rotation_deadband_deg: float = Field(2.0, ge=0, le=20)  # мелкие колебания курса карту не крутят
    compass_smoothing_s: float = Field(0.1, ge=0, le=2)  # компас на экране — без задержек, минимальное сглаживание
    # Зеркалить картинку на физическом экране слева направо (RV-50 сам зеркалит — он для камеры заднего вида)
    mirror_x: bool = False
    layers: MapLayers = MapLayers()
    show_trail: bool = True
    trail_seconds: int = Field(60, ge=0, le=600)
    force_mode: Literal["auto", "map", "camera"] = "auto"
    radio_panel: bool = True  # плашка радио (станция, трек) на экране, пока играет радио
    show_clock: bool = True  # часы, день недели и дата в левом верхнем углу экрана
    weather_panel: bool = True  # погода (сейчас + 2 дня) на месте плашки радио, пока радио выключено
    wind_on_map: bool = True  # ветер вокруг машины на карте (анимация)
    speed_limit: bool = True  # знак разрешённой скорости у спидометра (OSM, data_dir/speedlimits.sqlite)
    # На охране (alarm armed) вместо карты и HUD — чёрный экран с ALARM ON и часами, перерисовка раз в секунду
    armed_standby: bool = True


class ZoomStep(_Model):
    min_mps: float = Field(ge=0)  # от этой скорости и выше …
    zoom: float = Field(ge=1, le=22)  # … такой масштаб


class NavSettings(_Model):
    camera_below_mps: float = Field(2.0, ge=0)  # ниже — показываем камеру
    map_above_mps: float = Field(2.5, ge=0)  # выше — возвращаем карту (гистерезис)
    switch_delay_s: float = Field(2.0, ge=0)  # сколько скорость должна держаться для переключения
    zoom_delay_s: float = Field(3.0, ge=0)  # сколько держаться новой ступени зума
    zoom_table: list[ZoomStep] = [
        ZoomStep(min_mps=0.0, zoom=19),
        ZoomStep(min_mps=5.0, zoom=18),
        ZoomStep(min_mps=11.0, zoom=17),
        ZoomStep(min_mps=17.0, zoom=16),
        ZoomStep(min_mps=25.0, zoom=15),
    ]
    no_fix_mode: Literal["camera", "map"] = "camera"


class HeadingSettings(_Model):
    gps_above_mps: float = Field(3.0, ge=0)  # выше — курс по GPS
    blend_band_mps: float = Field(1.0, ge=0)  # ширина зоны плавного перехода компас↔GPS
    smoothing_s: float = Field(0.15, ge=0)  # постоянная времени сглаживания курса
    # На ходу запоминать поправку «GPS-курс − компас», на стоянке курс = компас + поправка (без скачка)
    anchor_to_gps: bool = True
    anchor_tau_s: float = Field(5.0, gt=0, le=60)


class LoresSettings(_Model):
    w: int = 768
    h: int = 576
    fps: int = Field(15, ge=1, le=30)
    jpeg_quality: int = Field(70, ge=10, le=95)


class CameraSettings(_Model):
    sensor_w: int = 2592  # режим сенсора (полный кадр = максимальный угол)
    sensor_h: int = 1944
    out_w: int = 1440  # что кодируем в H.264 (макс. для аппаратного кодера при 4:3)
    out_h: int = 1080
    fps: int = Field(15, ge=1, le=30)
    bitrate_kbps: int = Field(6000, ge=500, le=25000)
    gop: int = Field(15, ge=1)  # ключевой кадр каждые N кадров (15 = раз в секунду)
    # живое видео по требованию (/api/camera/live.ts): отдельный энкодер lores, только пока есть зрители
    live_bitrate_kbps: int = Field(800, ge=200, le=5000)
    segment_minutes: int = Field(10, ge=1, le=60)
    fsync_interval_s: float = Field(1.0, gt=0, le=10)
    hflip: bool = False
    vflip: bool = False
    exposure_mode: Literal["normal", "short", "long"] = "normal"
    ev: float = Field(0.0, ge=-4, le=4)
    # ночь: когда штатных 1/fps × 8 мало, AE удлиняет выдержку до предела и поднимает усиление до 16×;
    # выдержка длиннее кадра (1000/fps мс) снижает частоту кадров. Днём ничего не меняется.
    max_exposure_ms_driving: int = Field(66, ge=10, le=1000)  # едем (или скорость неизвестна)
    max_exposure_ms_parked: int = Field(125, ge=10, le=1000)  # стоим
    overlay_timestamp: bool = False
    lores: LoresSettings = LoresSettings()


class RearCameraSettings(_Model):
    """Камера заднего вида: IP-камера по RTSP (H.264) в той же сети Wi-Fi (docs/contracts/camera.md «Задняя камера»).
    Поток не перекодируется: запись, живое видео и публикация в MediaMTX — тот же H.264. Пароль — не здесь, а в
    secrets.env (FNAV_REAR_CAMERA_PASSWORD), его задают через POST /api/camera/rear/credentials."""

    enabled: bool = False
    url: str = Field("", max_length=512)  # rtsp://адрес[:порт]/путь — основной поток, без логина и пароля
    username: str = Field("", max_length=128)
    # второй поток камеры (меньше разрешение): экран в машине, снимки, распознавание; пусто — основной
    lores_url: str = Field("", max_length=512)
    transport: Literal["tcp", "udp"] = "tcp"
    record: bool = True  # писать на диск (иначе только живое видео)
    fps: int = Field(25, ge=1, le=60)  # ожидаемая частота (пока камера не сообщила свою)
    bitrate_kbps: int = Field(4000, ge=200, le=25000)  # оценка — запас места под следующий сегмент
    mirror: bool = True  # на экране в машине — зеркально, как в зеркале заднего вида
    audio: bool = True  # микрофон камеры: звук в живом видео админки, громкие звуки на охране (отметки с клипом)
    record_audio: bool = True  # звук и в записях (дорожка AAC 32 кбит/с: ~14 МБ в час)
    # высота подгоняется под пропорции потока; 25 к/с — задний вид на экране: кадры PAL-кинескопа (50 полей/с)
    lores: LoresSettings = LoresSettings(w=768, h=432, fps=25)


class StorageSettings(_Model):
    tile_cache_mb: int = Field(1024, ge=50)
    min_free_gb: float = Field(2.0, ge=0.5)  # столько свободного места держим всегда (резерв системы)
    max_recordings_gb: float = Field(10.0, ge=1)  # все записи вместе (с защищёнными); старые удаляются


class TileSettings(_Model):
    esri_api_key: str = ""  # пусто = старые адреса без ключа
    max_zoom: int = Field(21, ge=10, le=23)  # самый подробный уровень снимков (у Esri в Варшаве есть z20–z21)
    imagery_url: str = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
    )
    transport_url: str = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation"
        "/MapServer/tile/{z}/{y}/{x}"
    )
    places_url: str = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places"
        "/MapServer/tile/{z}/{y}/{x}"
    )
    street_url: str = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"
    )


class GpsSettings(_Model):
    device: str = "/dev/gps0"  # PL011 UART5 на GPIO12/13 (pi/udev/90-fnav.rules)
    baud: int = 115200
    rate_hz: int = Field(10, ge=1, le=18)
    constellations: list[Literal["gps", "glonass", "galileo", "beidou"]] = ["gps", "glonass", "galileo"]
    dyn_model: Literal["portable", "automotive", "airborne1g"] = "automotive"
    # Фильтр «стоим на месте»: ниже stationary_below_mps скорость = 0 и GPS-курс отбрасывается; снова «едем» —
    # когда скорость выше moving_above_mps держится moving_confirm_s. При HDOP > 3 пороги удваиваются.
    stationary_below_mps: float = Field(0.8, ge=0, le=5)
    moving_above_mps: float = Field(1.5, ge=0, le=10)
    moving_confirm_s: float = Field(1.0, ge=0, le=10)
    # Static Hold в самом приёмнике u-blox (UBX-CFG-NAV5): ниже порога позиция «замораживается», скорость = 0
    static_hold_cm_s: int = Field(50, ge=0, le=255)  # 0 = выключено
    static_hold_max_dist_m: int = Field(20, ge=0, le=65535)


class CompassCalibration(_Model):
    offset: list[float] = [0.0, 0.0, 0.0]  # hard iron, мкТл
    matrix: list[list[float]] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]  # soft iron
    fitted_at: str | None = None  # ISO-время калибровки
    quality: float | None = None  # 0..1, оценка качества подгонки
    # Остаточная девиация относительно GPS-курса (как у судовых компасов), градусы:
    # δ(θ) = A + B·sinθ + C·cosθ + D·sin2θ + E·cos2θ, где θ — магнитный курс после offset/matrix.
    # Истинный курс = θ + склонение + δ(θ) + mount_offset_deg. Пусто = не применять.
    deviation: list[float] = []  # [A, B, C, D, E]
    source: Literal["manual", "auto"] | None = None
    samples: int = 0
    coverage_pct: float = 0.0  # покрытие направлений (по GPS-курсу), %
    rms_deg: float | None = None  # СКО курса относительно GPS после калибровки


class AutoCalStatus(_Model):
    """Состояние автокалибровки компаса по GPS-курсу (в Telemetry.compass_cal)."""

    enabled: bool = True
    state: Literal["disabled", "collecting", "fitted", "error"] = "collecting"
    samples: int = 0  # принятых точек в текущем наборе
    coverage_pct: float = 0.0  # покрытие 36 секторов курса, %
    sectors: list[int] = []  # 36 значений: число точек в каждом 10°-секторе GPS-курса (0° = север)
    rms_deg: float | None = None  # СКО текущей подгонки
    applied: bool = False  # используется ли автокалибровка сейчас
    last_saved: str | None = None  # ISO-время последнего сохранения в настройки
    message: str = ""  # человекочитаемо, по-английски


class CompassSettings(_Model):
    chip: Literal["auto", "ist8310", "qmc5883l", "hmc5883l"] = "auto"
    i2c_bus: int = 1
    address: int | None = None  # None = по умолчанию для чипа (IST8310 = 0x0E)
    rate_hz: int = Field(25, ge=1, le=100)
    mount_offset_deg: float = Field(0.0, ge=-180, le=180)
    declination: Literal["auto"] | float = "auto"
    calibration: CompassCalibration = CompassCalibration()
    axes: str = ""  # переопределение осей, например "-x,-y,-z"; пусто = по умолчанию для чипа
    # Автокалибровка по GPS-курсу во время езды
    auto_calibrate: bool = True
    auto_min_speed_mps: float = Field(5.0, ge=1)  # точки собираются только быстрее этого
    auto_max_turn_rate_dps: float = Field(3.0, gt=0)  # и только почти по прямой
    auto_max_hdop: float = Field(2.0, gt=0)  # и только при хорошем фиксе
    auto_min_coverage_pct: float = Field(60.0, ge=10, le=100)  # применять, когда покрыто столько направлений
    auto_save_interval_s: float = Field(300.0, ge=10)  # как часто сохранять улучшенную калибровку


class GpioSettings(_Model):
    button_gpio: int | None = 23  # SAFETY_SWITCH модуля
    led_gpio: int | None = 24  # SAFETY_SWITCH_LED
    buzzer_gpio: int | None = None  # через MOSFET; None = не подключена
    button_active_low: bool | None = None  # None = определить автоматически
    led_active_low: bool | None = None
    long_press_s: float = 2.0


class TrackingSettings(_Model):
    """Журнал позиций (хранится на сервере, см. docs/contracts/remote.md)."""

    enabled: bool = True
    interval_s: float = Field(60.0, ge=10, le=3600)  # как часто брать точку
    max_hdop: float = Field(5.0, gt=0)  # точки с худшим HDOP не пишем


# следующая станция с клавиатуры (клавиши 1/2): по списку жанра, случайная из него, по избранному
RadioMode = Literal["order", "random", "favorites"]


class RadioSettings(_Model):
    """Интернет-радио (docs/contracts/radio.md)."""

    volume: int = Field(60, ge=0, le=100)
    resume_on_boot: bool = True  # после перезагрузки продолжить станцию, если радио играло
    mode: RadioMode = "order"  # клавиша 6 переключает по кругу

    @model_validator(mode="before")
    @classmethod
    def _old_shuffle(cls, d):  # до избранного было shuffle: bool
        if isinstance(d, dict) and "mode" not in d and d.get("shuffle") is True:
            d = {**d, "mode": "random"}
        return d


# действия клавиш мини-клавиатуры (fnav/core/keypad.py); "none" — клавиша ничего не делает
KeypadAction = Literal["none", "station_prev", "station_next", "radio_toggle", "shuffle", "favorite", "volume_down",
                       "volume_up", "view_toggle", "video_toggle", "camera_switch", "protect", "alarm"]


class KeypadSettings(_Model):
    """Мини-клавиатура (6 клавиш + крутилка): какое действие у каждой. Сама клавиатура всегда шлёт F13–F21 и Enter —
    раскладка меняется здесь, без перепрошивки. Клавиши — слева направо, сверху вниз; крутилка справа.
    *_hold — удержание (дольше 0.8 с) вместо нажатия; "none" — у клавиши только нажатие (срабатывает сразу).
    Пока удержаний не задавали — «в избранное» на удержании той клавиши, что переключает режим радио (shuffle)."""

    key1: KeypadAction = "station_prev"
    key2: KeypadAction = "station_next"
    key3: KeypadAction = "view_toggle"
    key4: KeypadAction = "camera_switch"
    key5: KeypadAction = "protect"
    key6: KeypadAction = "shuffle"
    knob_left: KeypadAction = "volume_down"
    knob_right: KeypadAction = "volume_up"
    knob_press: KeypadAction = "radio_toggle"
    key1_hold: KeypadAction = "none"
    key2_hold: KeypadAction = "none"
    key3_hold: KeypadAction = "none"
    key4_hold: KeypadAction = "none"
    key5_hold: KeypadAction = "none"
    key6_hold: KeypadAction = "none"
    knob_press_hold: KeypadAction = "none"

    @model_validator(mode="before")
    @classmethod
    def _favorite_on_mode_key(cls, d):
        if isinstance(d, dict) and not any(k.endswith("_hold") for k in d):
            for place in ("key1", "key2", "key3", "key4", "key5", "key6", "knob_press"):
                if d.get(place, cls.model_fields[place].default) == "shuffle":
                    return {**d, place + "_hold": "favorite"}
        return d


class BluetoothSettings(_Model):
    enabled: bool = True  # включать адаптер при старте
    auto_connect: bool = True  # переподключать колонку по умолчанию каждые 30 с


class HazardSettings(_Model):
    """Аварийка: RC-реле параллельно её кнопке (с фиксацией), замкнуто, пока мигаем (docs/contracts/alarm.md)."""

    enabled: bool = False  # мигать аварийкой при тревоге
    on_us: int = Field(2000, ge=500, le=2500)  # импульс «вкл», мкс
    off_us: int = Field(1000, ge=500, le=2500)  # импульс «выкл», мкс
    seconds: int = Field(30, ge=5, le=900)  # сколько мигать после события
    confirm: bool = False  # мигнуть 1 раз при Arm и 2 раза при Disarm


class VisionSettings(_Model):
    """Камера на охране (fnav-watch, docs/contracts/vision.md): человек подходит / лицо у стекла / стоит у фургона.
    Без сирены: push, аварийка, защита записи."""

    enabled: bool = False
    approach: bool = True  # человек подошёл: рамка росла, нижний край дошёл до ближней линии
    near_line: float = Field(0.6, ge=0.2, le=1.0)  # ближняя линия, доля высоты кадра сверху (над капотом)
    approach_hold_s: float = Field(2.0, ge=0, le=30)  # столько у линии — тревога (прохожие не успевают)
    face: bool = True  # крупное лицо у центра кадра
    face_min: float = Field(0.06, ge=0.02, le=0.5)  # ширина лица — доля ширины кадра (широкий объектив: 0.06 ≈ 1,3 м)
    face_hold_s: float = Field(3.0, ge=0.5, le=30)
    loiter: bool = True  # человек стоит у фургона (ночью — и по силуэту, который нейросеть потеряла, если видела)
    loiter_s: float = Field(8.0, ge=3, le=120)  # столько рядом у центра — тревога (раз за присутствие)
    # задняя камера (если включена, rear_camera.enabled): те же правила по её кадрам; ближняя линия — своя
    rear: bool = True
    rear_near_line: float = Field(0.6, ge=0.2, le=1.0)
    # отметки без тревоги: движение / машина / прохожий — серые метки на шкале записи (не push, не аварийка)
    marks: bool = True
    # громкие звуки у задней камеры (её микрофон, rear_camera.audio), пока стоит охрана: отметка с клипом звука
    sound: bool = True


class AlarmSettings(_Model):
    """Охрана: датчик удара MPU-6050, наклон, сдвиг по GPS (docs/contracts/alarm.md)."""

    impact_g: float = Field(0.15, ge=0.02, le=2.0)  # порог удара, g
    tilt_enabled: bool = True
    tilt_deg: float = Field(3.0, ge=0.5, le=45)  # порог наклона, °
    move_enabled: bool = True
    move_m: int = Field(100, ge=20, le=5000)  # порог сдвига по GPS, м
    siren: bool = True  # сирена через колонку Bluetooth
    siren_s: int = Field(30, ge=5, le=300)
    arm_delay_s: int = Field(60, ge=0, le=600)  # задержка постановки на охрану
    push_cooldown_s: int = Field(60, ge=0, le=3600)  # push одного вида не чаще
    notify_offline: bool = False  # сервер шлёт push, если устройство на охране пропало из сети
    # камера пропала: видео, которое было на охране, не идёт дольше video_loss_s → тревога (без сирены)
    video_loss: bool = True
    video_loss_s: int = Field(20, ge=5, le=600)
    sensor_bus: int = Field(3, ge=0, le=30)  # шина I2C: 3 — своя (pins 7/29); не нашёлся — ищем и на 1 (с компасом)
    sensor_address: Literal[104, 105] = 104  # 0x68 (AD0 → GND) или 0x69
    hazard: HazardSettings = HazardSettings()
    vision: VisionSettings = VisionSettings()


LevelAxis = Literal["x+", "x-", "y+", "y-", "z+", "z-"]


class LevelRef(_Model):
    """Эталон: замер (среднее a за 3 с, g, оси датчика) и введённые по уровню на полу углы."""

    a: list[float] = Field(min_length=3, max_length=3)
    pitch_deg: float = Field(ge=-45, le=45)
    roll_deg: float = Field(ge=-45, le=45)


class LevelCalibration(_Model):
    """Калибровка уровня (docs/contracts/level.md): векторы в осях датчика, нормированы.
    method "single"/"two_point" — от прежней версии (замер «ноля»), принимаются при загрузке."""

    up: list[float] = Field(min_length=3, max_length=3)
    forward: list[float] = Field(min_length=3, max_length=3)
    method: Literal["reference", "single", "two_point"] = "reference"
    forward_source: Literal["axis", "driving"] = "axis"
    axis: LevelAxis | None = "x+"
    ref: LevelRef | None = None  # есть — при смене forward up пересчитывается из него
    ts: float = 0.0


class LevelSettings(_Model):
    """Уровень: крен и тангаж по MPU-6050, подкладки под колёса (docs/contracts/level.md)."""

    show_on_screen: bool = True  # виджет на экране вместо спидометра, когда машина стоит
    wheelbase_m: float = Field(3.5, ge=1.5, le=7.0)
    track_m: float = Field(1.7, ge=1.0, le=3.0)
    calibration: LevelCalibration | None = None


SETTINGS_VERSION = 2  # 2: клавиша 4 — переключение камер вместо видеовыхода (fnav/core/config.py migrate)


class Settings(_Model):
    version: int = SETTINGS_VERSION
    display: DisplaySettings = DisplaySettings()
    nav: NavSettings = NavSettings()
    heading: HeadingSettings = HeadingSettings()
    camera: CameraSettings = CameraSettings()
    rear_camera: RearCameraSettings = RearCameraSettings()
    storage: StorageSettings = StorageSettings()
    tiles: TileSettings = TileSettings()
    gps: GpsSettings = GpsSettings()
    compass: CompassSettings = CompassSettings()
    gpio: GpioSettings = GpioSettings()
    tracking: TrackingSettings = TrackingSettings()
    radio: RadioSettings = RadioSettings()
    bluetooth: BluetoothSettings = BluetoothSettings()
    keypad: KeypadSettings = KeypadSettings()
    alarm: AlarmSettings = AlarmSettings()
    level: LevelSettings = LevelSettings()


# ───────────────────────────── Датчики ─────────────────────────────


class GpsFix(_Model):
    fix: Literal["none", "2d", "3d"] = "none"
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None
    speed_mps: float | None = None
    course_deg: float | None = None  # курс над землёй (COG), 0..360, от истинного севера
    hdop: float | None = None
    sats_used: int = 0
    sats_visible: int = 0
    time_utc: str | None = None  # ISO 8601 из GPS
    age_s: float | None = None  # сколько секунд назад было последнее обновление


class Satellite(_Model):
    gnss: Literal["gps", "glonass", "galileo", "beidou", "sbas", "qzss", "other"] = "other"
    prn: int
    el: float | None = None  # возвышение, °
    az: float | None = None  # азимут, °
    snr: float | None = None  # дБ-Гц
    used: bool = False


class Heading(_Model):
    deg: float | None = None  # итоговый курс, 0..360 от истинного севера
    source: Literal["gps", "mag", "blend", "none"] = "none"
    mag_deg: float | None = None  # по компасу (уже с калибровкой, склонением и поправкой установки)
    gps_course_deg: float | None = None
    declination_deg: float | None = None
    anchor_offset_deg: float | None = None  # текущая поправка компаса по GPS (CompassAnchor), °


class CompassRaw(_Model):
    x: float
    y: float
    z: float  # мкТл, после калибровки
    raw_x: float | None = None
    raw_y: float | None = None
    raw_z: float | None = None  # до калибровки
    chip: str | None = None
    ok: bool = True


class CalibrationStatus(_Model):
    active: bool = False
    samples: int = 0
    coverage_pct: float = 0.0  # насколько равномерно покрыты направления (0..100)
    message: str = ""


class SensorSnapshot(_Model):
    gps: GpsFix = GpsFix()
    heading: Heading = Heading()
    compass: CompassRaw | None = None
    compass_cal: AutoCalStatus | None = None
    satellites: list[Satellite] = []
    errors: list[str] = []  # человекочитаемые проблемы ("компас не отвечает" и т.п.)


# ───────────────────────────── Камера ─────────────────────────────


CameraId = Literal["front", "rear"]


class CameraStatus(_Model):
    # disabled — задняя камера выключена в настройках; live — работает, но запись выключена (rear_camera.record)
    state: Literal["starting", "recording", "live", "stopped", "error", "disabled"] = "stopped"
    camera: CameraId = "front"
    recording: bool = False
    current_segment: str | None = None  # путь относительно recordings_dir
    segment_started_utc: str | None = None
    fps_actual: float | None = None
    bitrate_kbps: float | None = None
    sensor_mode: str | None = None  # "2592x1944"
    output_size: str | None = None  # "1440x1080"
    frames_dropped: int = 0
    error: str | None = None
    recordings_count: int = 0
    recordings_gb: float = 0.0
    free_gb: float | None = None
    time_synced: bool = False  # было ли системное время синхронизировано при начале сегмента
    # экспозиция (метаданные кадра) и предел выдержки, который сейчас действует
    moving: bool | None = None  # что сообщило ядро: едем / стоим / ещё не сообщало
    max_exposure_ms: float | None = None
    exposure_ms: float | None = None
    gain: float | None = None  # аналоговое × цифровое
    lux: float | None = None  # оценка освещённости от libcamera
    # задняя (RTSP): что сообщила камера и есть ли пароль в secrets.env
    codec: str | None = None  # "h264"
    stream_fps: float | None = None  # частота потока по данным камеры
    password_set: bool | None = None
    # звук задней камеры: "pcm_mulaw 8000 Hz" (есть микрофон) или None; громкие звуки — пока охрана (sound_armed),
    # уровень за 1 с (dBFS), счётчик событий (sound_seq: новые — GET /sound/events?after=…)
    audio: str | None = None
    sound_armed: bool | None = None
    sound_level_db: float | None = None
    sound_seq: int | None = None
    # сколько секунд нет кадров камеры (охрана: видео пропало — тревога); None — с запуска конвейера кадров не было
    video_age_s: float | None = None


# ───────────────────────────── Телеметрия (WebSocket) ─────────────────────────────


class DisplayState(_Model):
    mode: Literal["map", "camera"] = "camera"
    reason: Literal["speed", "no_fix", "forced", "startup", "rear"] = "startup"
    # какая камера в режиме camera: rear — задний вид (кнопка «камеры»), поверх авто-логики карта/камера
    camera: CameraId = "front"
    rear_mirror: bool = True  # задний вид на экране — зеркально (rear_camera.mirror)
    target_zoom: float = 19
    orientation: Literal["heading_up", "north_up"] = "heading_up"
    gamepad: bool = False  # подключён геймпад: только тогда экран опрашивает его (меню игр)


class DiskStatus(_Model):
    total_gb: float | None = None
    free_gb: float | None = None
    recordings_gb: float | None = None
    tiles_mb: float | None = None


class SystemStatus(_Model):
    cpu_temp_c: float | None = None
    throttled: str | None = None  # вывод vcgencmd get_throttled, например "0x0"
    load_1m: float | None = None
    mem_used_pct: float | None = None
    uptime_s: float | None = None
    ip: str | None = None
    wifi_ssid: str | None = None
    wifi_signal_pct: int | None = None
    internet: bool | None = None  # доступны ли тайлы Esri
    time_synced: bool | None = None
    utc_offset_min: int | None = None  # часовой пояс устройства: часы на экране показывают его время


# ───────────────────────────── Радио и Bluetooth ─────────────────────────────


class RadioStation(_Model):
    """Станция каталога radio-browser.info."""

    uuid: str = ""
    name: str = "Radio"
    url: str = Field(..., pattern=r"^https?://", max_length=2048)  # только http(s): mpv не открывает файлы
    homepage: str = ""
    favicon: str = ""
    tags: str = ""
    country: str = ""
    countrycode: str = ""
    language: str = ""
    codec: str = ""
    bitrate: int = 0
    votes: int = 0
    clickcount: int = 0
    hls: bool = False


class RadioOutput(_Model):
    address: str
    name: str = ""
    connected: bool = False


RadioPhase = Literal["stopped", "connecting", "playing", "waiting_output", "error"]


class RadioState(_Model):
    state: RadioPhase = "stopped"
    station: RadioStation | None = None
    genre: str | None = None  # тег плитки, из которой выбрана станция
    track: str | None = None  # ICY StreamTitle («Artist - Title»); None — поток не сообщает
    volume: int = 60
    output: RadioOutput | None = None
    error: str | None = None
    recent: list[RadioStation] = []
    favorites: list[RadioStation] = []  # избранное: клавиша 6 (удержание) добавляет, удаляют в веб-интерфейсе


class SpeedLimit(_Model):
    """Разрешённая скорость на дороге под колёсами (fnav/core/speedlimit.py, OpenStreetMap офлайн)."""

    kmh: int
    src: Literal["osm", "zone", "default"] = "osm"  # знак в OSM / зона (PL:urban…) / по типу дороги
    way: int | None = None  # id дороги в OSM


class WeatherDay(_Model):
    date: str  # YYYY-MM-DD, местная дата точки прогноза
    weekday: str  # "MON"
    code: int | None = None  # код погоды WMO
    t_max: float | None = None
    t_min: float | None = None
    precip_prob: int | None = None  # вероятность осадков, %


class WeatherStatus(_Model):
    """Погода для экрана (fnav/core/weather.py): сообщение WebSocket {"type": "weather", …}."""

    type: Literal["weather"] = "weather"
    temp_c: float | None = None
    feels_c: float | None = None
    code: int | None = None
    is_day: bool = True
    wind_ms: float | None = None
    gust_ms: float | None = None
    wind_from_deg: float | None = None  # откуда дует, ° от севера
    days: list[WeatherDay] = []  # завтра и послезавтра
    updated: float | None = None  # когда получен прогноз, unix-время
    stale: bool = False  # прогноз старше 3 ч (нет интернета): показан почасовой прогноз из кэша
    lat: float | None = None
    lon: float | None = None
    source: str = "Open-Meteo"


class RadioStatus(_Model):
    """Компактное состояние радио в телеметрии (для экрана)."""

    state: RadioPhase = "stopped"
    station: str | None = None
    track: str | None = None
    genre: str | None = None  # подпись плитки или первый тег станции
    volume: int = 60
    output: str | None = None
    output_connected: bool = False
    mode: RadioMode = "order"  # следующая станция: по порядку / случайная / по избранному (settings.radio.mode)
    favorite: bool = False  # текущая станция — в избранном
    favorites: int = 0  # сколько станций в избранном
    fav_seq: int = 0  # +1 при каждом «в избранное» с клавиатуры (экран: FAVORITE SAVED / ALREADY SAVED)


class BtDevice(_Model):
    address: str
    name: str = ""
    icon: str = ""
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    rssi: int | None = None
    audio: bool = False


class BtState(_Model):
    available: bool = False
    powered: bool = False
    discovering: bool = False
    audio_device: str | None = None
    devices: list[BtDevice] = []
    error: str | None = None


# ───────────────────────────── Охрана и уровень (MPU-6050) ─────────────────────────────

AlarmStateName = Literal["off", "arming", "armed", "triggered"]
AlarmKind = Literal["impact", "tilt", "move", "approach", "face", "loiter", "video_lost", "video_back", "test"]


class AlarmEvent(_Model):
    """Событие охраны: журнал, push (через fnav-relay), телеметрия (docs/contracts/alarm.md §4)."""

    id: str  # uuid4 hex
    ts: float  # unix-время
    kind: AlarmKind
    title: str
    body: str
    url: str = "#/alarm"  # относительно каталога админки
    data: dict = {}  # peak_g / tilt_deg / distance_m, lat, lon; segment — защищённая запись камеры


class AlarmSensor(_Model):
    ok: bool = False
    model: str | None = None  # "MPU-6050", клон — с WHO_AM_I
    address: int = 104
    bus: int | None = None  # шина I2C, на которой нашёлся
    error: str | None = None


class AlarmRelay(_Model):
    configured: bool = False
    pending: int = 0  # событий в outbox


class AlarmHazard(_Model):
    enabled: bool = False
    active: bool = False  # мигает сейчас
    error: str | None = None  # реле не открылось / ошибка вывода


class AlarmVisionCamera(_Model):
    """Одна камера в fnav-watch (задняя; передняя — поля самого AlarmVision)."""

    watching: bool = False  # смотрит в её кадр
    people: int = 0  # людей в её кадре сейчас
    error: str | None = None  # например, нет кадров задней камеры (передняя при этом работает)


class AlarmVision(_Model):
    """fnav-watch глазами ядра (docs/contracts/vision.md)."""

    enabled: bool = False  # settings.alarm.vision.enabled
    available: bool = False  # сервис отвечает
    watching: bool = False  # смотрит в кадр (охрана включена)
    people: int = 0  # людей в кадре сейчас (всех камер)
    error: str | None = None  # сервис или передняя камера; ошибка задней — в rear.error
    rear: AlarmVisionCamera | None = None  # смотрит и заднюю (alarm.vision.rear и rear_camera.enabled), иначе нет
    # передняя отдельно: её ошибка (нет кадров) — не ошибка сервиса, задняя при этом смотрится (старый сервис — None)
    front: AlarmVisionCamera | None = None


class AlarmCameraVideo(_Model):
    """Идёт ли видео камеры (охрана: пропало — тревога video_lost). Задняя — только если включена."""

    camera: CameraId
    video: bool = False  # кадры идут сейчас
    lost: bool = False  # на охране пропало дольше video_loss_s (было video_lost) и ещё не вернулось


class AlarmStatus(_Model):
    state: AlarmStateName = "off"
    armed_at: float | None = None
    arm_in_s: int | None = None  # обратный отсчёт в arming
    sensor: AlarmSensor = AlarmSensor()
    level_g: float | None = None  # максимум |a − g| за интервал телеметрии
    tilt_deg: float | None = None  # текущий угол от базы наклона (null вне armed)
    siren: bool = False  # звучит
    events: list[AlarmEvent] = []  # новые первыми, ≤ 100
    relay: AlarmRelay = AlarmRelay()
    hazard: AlarmHazard = AlarmHazard()
    vision: AlarmVision = AlarmVision()
    cameras: list[AlarmCameraVideo] = []


class AlarmLast(_Model):
    kind: AlarmKind
    ts: float
    title: str


class AlarmTelemetry(_Model):
    state: AlarmStateName = "off"
    arm_in_s: int | None = None
    level_g: float | None = None
    sensor_ok: bool = False
    siren: bool = False
    hazard: bool = False  # аварийка мигает
    seq: int = 0  # +1 на каждое событие журнала
    last: AlarmLast | None = None


class WheelsCm(_Model):
    """Сколько подложить под колесо, см (самое высокое — 0)."""

    fl: float
    fr: float
    rl: float
    rr: float


class LevelWizard(_Model):
    learning: bool = False  # учим «вперёд» по разгонам/торможениям
    learn_s: float = 0.0
    learn_spread_deg: float | None = None


class LevelStatus(_Model):
    sensor_ok: bool = False
    calibrated: bool = False
    pitch_deg: float | None = None  # > 0 — перед выше зада
    roll_deg: float | None = None  # > 0 — правый борт выше левого
    still: bool = False
    raw: list[float] | None = None  # сглаженный вектор, g, оси датчика
    wheels_cm: WheelsCm | None = None
    calibration: LevelCalibration | None = None
    wizard: LevelWizard = LevelWizard()


class LevelTelemetry(_Model):
    sensor_ok: bool = False
    calibrated: bool = False
    pitch_deg: float | None = None
    roll_deg: float | None = None
    wheels_cm: WheelsCm | None = None


class Telemetry(_Model):
    type: Literal["telemetry"] = "telemetry"
    t: float  # unix-время сервера, с
    gps: GpsFix
    heading: Heading
    compass: CompassRaw | None = None
    compass_cal: AutoCalStatus | None = None
    display: DisplayState
    camera: CameraStatus
    camera_rear: CameraStatus | None = None  # None — задняя камера выключена в настройках
    disk: DiskStatus = DiskStatus()
    system: SystemStatus = SystemStatus()
    radio: RadioStatus | None = None
    speed_limit: SpeedLimit | None = None
    alarm: AlarmTelemetry | None = None
    level: LevelTelemetry | None = None
    errors: list[str] = []
