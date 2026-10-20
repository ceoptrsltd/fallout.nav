from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from fnav.sensors.declination import decimal_year, declination_deg, field_nT


@pytest.mark.parametrize("year", [2025.5, 2026.0, 2026.8])
def test_moscow(year: float) -> None:
    d = declination_deg(55.7558, 37.6173, year)
    assert 11.0 <= d <= 13.0, d  # NOAA: ≈ +12° в 2025–2026


@pytest.mark.parametrize(
    "lat,lon,expected,tol",
    [
        (40.015, -105.27, 7.7, 1.0),  # Боулдер
        (51.5, -0.13, 1.0, 1.0),  # Лондон
        (35.68, 139.69, -7.9, 1.0),  # Токио
        (-33.87, 151.21, 12.8, 1.0),  # Сидней
        (55.03, 82.92, 8.3, 1.5),  # Новосибирск
    ],
)
def test_known_places(lat: float, lon: float, expected: float, tol: float) -> None:
    assert declination_deg(lat, lon, 2025.5) == pytest.approx(expected, abs=tol)


def test_field_magnitude_moscow() -> None:
    x, y, z = field_nT(55.75, 37.62, 0.0, 2025.0)
    f = math.sqrt(x * x + y * y + z * z)
    assert 50000 < f < 56000  # полное поле ≈ 53 мкТл
    assert z > 0  # северное полушарие — вниз
    assert 14000 < math.hypot(x, y) < 18000


def test_decimal_year() -> None:
    assert decimal_year(datetime(2026, 1, 1, tzinfo=UTC)) == 2026.0
    assert decimal_year(datetime(2026, 7, 2, 12, tzinfo=UTC)) == pytest.approx(2026.5, abs=0.01)
