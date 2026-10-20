"""Магнитное склонение по модели WMM2025 (NOAA/NCEI, действует 2025.0–2030.0).

Собственная компактная реализация (сферические гармоники до 12-й степени), только stdlib.
Коэффициенты — файл WMM.COF (WMM-2025, 11/13/2024), столбцы: n m g h dg dh (нТл, нТл/год).
Точность модели по склонению ~0.3–0.5° в средних широтах — для компаса машины с запасом.
За пределами 2025–2030 считаем с ограничением года (вековой ход линеен, ошибка растёт медленно).

    declination_deg(55.75, 37.62)  # Москва → ≈ +12°
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from functools import lru_cache

EPOCH = 2025.0
VALID_UNTIL = 2030.0

_WMM2025 = """
1 0 -29351.8 0.0 12.0 0.0
1 1 -1410.8 4545.4 9.7 -21.5
2 0 -2556.6 0.0 -11.6 0.0
2 1 2951.1 -3133.6 -5.2 -27.7
2 2 1649.3 -815.1 -8.0 -12.1
3 0 1361.0 0.0 -1.3 0.0
3 1 -2404.1 -56.6 -4.2 4.0
3 2 1243.8 237.5 0.4 -0.3
3 3 453.6 -549.5 -15.6 -4.1
4 0 895.0 0.0 -1.6 0.0
4 1 799.5 278.6 -2.4 -1.1
4 2 55.7 -133.9 -6.0 4.1
4 3 -281.1 212.0 5.6 1.6
4 4 12.1 -375.6 -7.0 -4.4
5 0 -233.2 0.0 0.6 0.0
5 1 368.9 45.4 1.4 -0.5
5 2 187.2 220.2 0.0 2.2
5 3 -138.7 -122.9 0.6 0.4
5 4 -142.0 43.0 2.2 1.7
5 5 20.9 106.1 0.9 1.9
6 0 64.4 0.0 -0.2 0.0
6 1 63.8 -18.4 -0.4 0.3
6 2 76.9 16.8 0.9 -1.6
6 3 -115.7 48.8 1.2 -0.4
6 4 -40.9 -59.8 -0.9 0.9
6 5 14.9 10.9 0.3 0.7
6 6 -60.7 72.7 0.9 0.9
7 0 79.5 0.0 -0.0 0.0
7 1 -77.0 -48.9 -0.1 0.6
7 2 -8.8 -14.4 -0.1 0.5
7 3 59.3 -1.0 0.5 -0.8
7 4 15.8 23.4 -0.1 0.0
7 5 2.5 -7.4 -0.8 -1.0
7 6 -11.1 -25.1 -0.8 0.6
7 7 14.2 -2.3 0.8 -0.2
8 0 23.2 0.0 -0.1 0.0
8 1 10.8 7.1 0.2 -0.2
8 2 -17.5 -12.6 0.0 0.5
8 3 2.0 11.4 0.5 -0.4
8 4 -21.7 -9.7 -0.1 0.4
8 5 16.9 12.7 0.3 -0.5
8 6 15.0 0.7 0.2 -0.6
8 7 -16.8 -5.2 -0.0 0.3
8 8 0.9 3.9 0.2 0.2
9 0 4.6 0.0 -0.0 0.0
9 1 7.8 -24.8 -0.1 -0.3
9 2 3.0 12.2 0.1 0.3
9 3 -0.2 8.3 0.3 -0.3
9 4 -2.5 -3.3 -0.3 0.3
9 5 -13.1 -5.2 0.0 0.2
9 6 2.4 7.2 0.3 -0.1
9 7 8.6 -0.6 -0.1 -0.2
9 8 -8.7 0.8 0.1 0.4
9 9 -12.9 10.0 -0.1 0.1
10 0 -1.3 0.0 0.1 0.0
10 1 -6.4 3.3 0.0 0.0
10 2 0.2 0.0 0.1 -0.0
10 3 2.0 2.4 0.1 -0.2
10 4 -1.0 5.3 -0.0 0.1
10 5 -0.6 -9.1 -0.3 -0.1
10 6 -0.9 0.4 0.0 0.1
10 7 1.5 -4.2 -0.1 0.0
10 8 0.9 -3.8 -0.1 -0.1
10 9 -2.7 0.9 -0.0 0.2
10 10 -3.9 -9.1 -0.0 -0.0
11 0 2.9 0.0 0.0 0.0
11 1 -1.5 0.0 -0.0 -0.0
11 2 -2.5 2.9 0.0 0.1
11 3 2.4 -0.6 0.0 -0.0
11 4 -0.6 0.2 0.0 0.1
11 5 -0.1 0.5 -0.1 -0.0
11 6 -0.6 -0.3 0.0 -0.0
11 7 -0.1 -1.2 -0.0 0.1
11 8 1.1 -1.7 -0.1 -0.0
11 9 -1.0 -2.9 -0.1 0.0
11 10 -0.2 -1.8 -0.1 0.0
11 11 2.6 -2.3 -0.1 0.0
12 0 -2.0 0.0 0.0 0.0
12 1 -0.2 -1.3 0.0 -0.0
12 2 0.3 0.7 -0.0 0.0
12 3 1.2 1.0 -0.0 -0.1
12 4 -1.3 -1.4 -0.0 0.1
12 5 0.6 -0.0 -0.0 -0.0
12 6 0.6 0.6 0.1 -0.0
12 7 0.5 -0.1 -0.0 -0.0
12 8 -0.1 0.8 0.0 0.0
12 9 -0.4 0.1 0.0 -0.0
12 10 -0.2 -1.0 -0.1 -0.0
12 11 -1.3 0.1 -0.0 0.0
12 12 -0.7 0.2 -0.1 -0.1
"""

N_MAX = 12
_A = 6378.137  # WGS-84, км
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_RE = 6371.2  # опорный радиус модели, км


def _load() -> tuple[list[list[float]], list[list[float]], list[list[float]], list[list[float]]]:
    z = [[0.0] * (N_MAX + 1) for _ in range(N_MAX + 1)]
    g, h, gd, hd = (list(map(list, z)) for _ in range(4))
    for line in _WMM2025.strip().splitlines():
        n, m, a, b, c, d = line.split()
        ni, mi = int(n), int(m)
        g[ni][mi], h[ni][mi], gd[ni][mi], hd[ni][mi] = float(a), float(b), float(c), float(d)
    return g, h, gd, hd


_G, _H, _GD, _HD = _load()


def decimal_year(dt: datetime | None = None) -> float:
    dt = dt or datetime.now(UTC)
    start = datetime(dt.year, 1, 1, tzinfo=dt.tzinfo)
    end = datetime(dt.year + 1, 1, 1, tzinfo=dt.tzinfo)
    return dt.year + (dt - start).total_seconds() / (end - start).total_seconds()


def field_nT(lat: float, lon: float, alt_km: float = 0.0, year: float | None = None) -> tuple[float, float, float]:
    """Геодезические компоненты (X север, Y восток, Z вниз), нТл."""
    if year is None:
        year = decimal_year()
    t = min(max(year, EPOCH - 1.0), VALID_UNTIL + 2.0) - EPOCH
    phi = math.radians(lat)
    lam = math.radians(lon)
    sp, cp = math.sin(phi), math.cos(phi)
    rc = _A / math.sqrt(1 - _E2 * sp * sp)
    p = (rc + alt_km) * cp
    zz = (rc * (1 - _E2) + alt_km) * sp
    r = math.hypot(p, zz)
    phi_c = math.asin(zz / r)  # геоцентрическая широта
    ct = math.sin(phi_c)  # cos(колатитуды)
    st = max(math.cos(phi_c), 1e-10)

    # ассоциированные функции Лежандра (Гаусс) + производные по колатитуде; затем нормировка Шмидта
    P = [[0.0] * (N_MAX + 1) for _ in range(N_MAX + 1)]
    dP = [[0.0] * (N_MAX + 1) for _ in range(N_MAX + 1)]
    P[0][0] = 1.0
    for n in range(1, N_MAX + 1):
        for m in range(n + 1):
            if n == m:
                P[n][m] = st * P[n - 1][m - 1]
                dP[n][m] = st * dP[n - 1][m - 1] + ct * P[n - 1][m - 1]
            elif n == 1:
                P[n][m] = ct * P[n - 1][m]
                dP[n][m] = ct * dP[n - 1][m] - st * P[n - 1][m]
            else:
                k = ((n - 1) ** 2 - m * m) / ((2 * n - 1) * (2 * n - 3))
                P[n][m] = ct * P[n - 1][m] - k * P[n - 2][m]
                dP[n][m] = ct * dP[n - 1][m] - st * P[n - 1][m] - k * dP[n - 2][m]
    S = [[0.0] * (N_MAX + 1) for _ in range(N_MAX + 1)]
    S[0][0] = 1.0
    for n in range(1, N_MAX + 1):
        S[n][0] = S[n - 1][0] * (2 * n - 1) / n
        for m in range(1, n + 1):
            S[n][m] = S[n][m - 1] * math.sqrt((n - m + 1) * (2 if m == 1 else 1) / (n + m))

    xp = yp = zp = 0.0
    ar = _RE / r
    arn = ar * ar
    for n in range(1, N_MAX + 1):
        arn *= ar  # (a/r)^(n+2)
        for m in range(n + 1):
            g = _G[n][m] + t * _GD[n][m]
            h = _H[n][m] + t * _HD[n][m]
            cm, sm = math.cos(m * lam), math.sin(m * lam)
            pnm = P[n][m] * S[n][m]
            dpnm = dP[n][m] * S[n][m]
            gc = g * cm + h * sm
            xp += arn * gc * dpnm
            yp += arn * m * (g * sm - h * cm) * pnm / st
            zp -= arn * (n + 1) * gc * pnm
    # геоцентрические → геодезические
    psi = phi_c - phi
    x = xp * math.cos(psi) - zp * math.sin(psi)
    z = xp * math.sin(psi) + zp * math.cos(psi)
    return x, yp, z


@lru_cache(maxsize=256)
def _decl_cached(lat_r: float, lon_r: float, year_r: float) -> float:
    x, y, _ = field_nT(lat_r, lon_r, 0.0, year_r)
    return math.degrees(math.atan2(y, x))


def declination_deg(lat: float, lon: float, year: float | None = None) -> float:
    """Склонение, ° (восточное +). Кэш по сетке 0.1° и 0.1 года — пересчёт дешёвый, но не на каждый кадр."""
    if year is None:
        year = decimal_year()
    return _decl_cached(round(lat, 1), round(lon, 1), round(year, 1))
