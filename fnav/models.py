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

from pydantic import BaseModel, ConfigDict, Field


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
    layers: MapLayers = MapLayers()
    show_trail: bool = True
    trail_seconds: int = Field(60, ge=0, le=600)
    force_mode: Literal["auto", "map", "camera"] = "auto"


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
    smoothing_s: float = Field(0.5, ge=0)  # постоянная времени сглаживания курса


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
    segment_minutes: int = Field(10, ge=1, le=60)
    fsync_interval_s: float = Field(1.0, gt=0, le=10)
    hflip: bool = False
    vflip: bool = False
    exposure_mode: Literal["normal", "short", "long"] = "normal"
    ev: float = Field(0.0, ge=-4, le=4)
    overlay_timestamp: bool = False
    lores: LoresSettings = LoresSettings()


class StorageSettings(_Model):
    tile_cache_mb: int = Field(1024, ge=50)
    min_free_gb: float = Field(2.0, ge=0.5)  # столько свободного места держим всегда (резерв системы)


class TileSettings(_Model):
    esri_api_key: str = ""  # пусто = старые адреса без ключа
    max_zoom: int = Field(19, ge=10, le=23)
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
    device: str = "/dev/ttyAMA0"
    baud: int = 115200
    rate_hz: int = Field(10, ge=1, le=18)
    constellations: list[Literal["gps", "glonass", "galileo", "beidou"]] = ["gps", "glonass", "galileo"]
    dyn_model: Literal["portable", "automotive", "airborne1g"] = "automotive"


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


class Settings(_Model):
    version: int = 1
    display: DisplaySettings = DisplaySettings()
    nav: NavSettings = NavSettings()
    heading: HeadingSettings = HeadingSettings()
    camera: CameraSettings = CameraSettings()
    storage: StorageSettings = StorageSettings()
    tiles: TileSettings = TileSettings()
    gps: GpsSettings = GpsSettings()
    compass: CompassSettings = CompassSettings()
    gpio: GpioSettings = GpioSettings()


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


class CameraStatus(_Model):
    state: Literal["starting", "recording", "stopped", "error"] = "stopped"
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


# ───────────────────────────── Телеметрия (WebSocket) ─────────────────────────────


class DisplayState(_Model):
    mode: Literal["map", "camera"] = "camera"
    reason: Literal["speed", "no_fix", "forced", "startup"] = "startup"
    target_zoom: float = 19
    orientation: Literal["heading_up", "north_up"] = "heading_up"


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


class Telemetry(_Model):
    type: Literal["telemetry"] = "telemetry"
    t: float  # unix-время сервера, с
    gps: GpsFix
    heading: Heading
    compass: CompassRaw | None = None
    compass_cal: AutoCalStatus | None = None
    display: DisplayState
    camera: CameraStatus
    disk: DiskStatus = DiskStatus()
    system: SystemStatus = SystemStatus()
    errors: list[str] = []
