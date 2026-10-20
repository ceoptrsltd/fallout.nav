// Значения по умолчанию — зеркало fnav.models.Settings() (используются, пока не пришли настройки
// от fnav-core). Источник правды — fnav/models.py; при расхождении побеждают настройки с сервера.

export const DEFAULT_SETTINGS = {
  "version": 1,
  "display": {
    "orientation": "heading_up",
    "compass_style": "v1",
    "screen": {
      "w_mm": 92.0,
      "h_mm": 69.0
    },
    "safe_area_pct": 5.0,
    "speed_units": "kmh",
    "vehicle_anchor_y": 0.67,
    "vehicle_rotation_lag_s": 0.25,
    "map_rotation_lag_s": 1.5,
    "map_rotation_deadband_deg": 2.0,
    "layers": {
      "imagery": true,
      "transport": true,
      "places": true,
      "street_map_instead": false
    },
    "show_trail": true,
    "trail_seconds": 60,
    "force_mode": "auto"
  },
  "nav": {
    "camera_below_mps": 2.0,
    "map_above_mps": 2.5,
    "switch_delay_s": 2.0,
    "zoom_delay_s": 3.0,
    "zoom_table": [
      {
        "min_mps": 0.0,
        "zoom": 19.0
      },
      {
        "min_mps": 5.0,
        "zoom": 18.0
      },
      {
        "min_mps": 11.0,
        "zoom": 17.0
      },
      {
        "min_mps": 17.0,
        "zoom": 16.0
      },
      {
        "min_mps": 25.0,
        "zoom": 15.0
      }
    ],
    "no_fix_mode": "camera"
  },
  "heading": {
    "gps_above_mps": 3.0,
    "blend_band_mps": 1.0,
    "smoothing_s": 0.5
  },
  "camera": {
    "sensor_w": 2592,
    "sensor_h": 1944,
    "out_w": 1440,
    "out_h": 1080,
    "fps": 15,
    "bitrate_kbps": 6000,
    "gop": 15,
    "segment_minutes": 10,
    "fsync_interval_s": 1.0,
    "hflip": false,
    "vflip": false,
    "exposure_mode": "normal",
    "ev": 0.0,
    "overlay_timestamp": false,
    "lores": {
      "w": 768,
      "h": 576,
      "fps": 15,
      "jpeg_quality": 70
    }
  },
  "storage": {
    "tile_cache_mb": 1024,
    "min_free_gb": 2.0
  },
  "tiles": {
    "esri_api_key": "",
    "max_zoom": 19,
    "imagery_url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "transport_url": "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}",
    "places_url": "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
    "street_url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"
  },
  "gps": {
    "device": "/dev/ttyAMA0",
    "baud": 115200,
    "rate_hz": 10,
    "constellations": [
      "gps",
      "glonass",
      "galileo"
    ],
    "dyn_model": "automotive"
  },
  "compass": {
    "chip": "auto",
    "i2c_bus": 1,
    "address": null,
    "rate_hz": 25,
    "mount_offset_deg": 0.0,
    "declination": "auto",
    "calibration": {
      "offset": [
        0.0,
        0.0,
        0.0
      ],
      "matrix": [
        [
          1.0,
          0.0,
          0.0
        ],
        [
          0.0,
          1.0,
          0.0
        ],
        [
          0.0,
          0.0,
          1.0
        ]
      ],
      "fitted_at": null,
      "quality": null
    }
  },
  "gpio": {
    "button_gpio": 23,
    "led_gpio": 24,
    "buzzer_gpio": null,
    "button_active_low": null,
    "led_active_low": null,
    "long_press_s": 2.0
  }
};

/** Глубокое слияние: объекты сливаются, массивы и значения заменяются. Аргументы не меняются. */
export function deepMerge(base, patch) {
  if (patch === null || typeof patch !== "object" || Array.isArray(patch)) return patch === undefined ? base : patch;
  const out = Array.isArray(base) || base === null || typeof base !== "object" ? {} : { ...base };
  for (const [k, v] of Object.entries(patch)) {
    out[k] = v !== null && typeof v === "object" && !Array.isArray(v) ? deepMerge(out[k], v) : v;
  }
  return out;
}
