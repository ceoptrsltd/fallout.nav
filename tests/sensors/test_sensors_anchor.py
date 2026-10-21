from fnav.sensors import heading as hd


def run(anchor, smoother, mag, gps, speed, seconds, dt=0.1, th=3.0, band=1.0):
    out = None
    for _ in range(int(seconds / dt)):
        if gps is not None and hd.gps_weight(speed, th, band) >= 1.0:
            anchor.learn(mag, gps, dt)
        deg, _src = hd.fuse(anchor.apply(mag), gps, speed, th, band)
        out = smoother.update(deg, dt)
    return out


def test_no_jump_when_stopping_with_uncalibrated_compass():
    a, sm = hd.CompassAnchor(tau_s=5.0), hd.HeadingSmoother(0.15)
    # едем на курс 100° по GPS, компас врёт на +40° (показывает 140°)
    moving = run(a, sm, mag=140.0, gps=100.0, speed=10.0, seconds=30)
    assert abs(hd.angle_diff(moving, 100.0)) < 0.5
    # остановились: GPS-курса нет, курс по компасу + поправка — скачка на 40° нет
    stopped = run(a, sm, mag=140.0, gps=None, speed=0.0, seconds=5)
    assert abs(hd.angle_diff(stopped, 100.0)) < 1.0
    # на парковке повернули на 30° — курс следует за компасом относительно
    turned = run(a, sm, mag=170.0, gps=None, speed=0.0, seconds=5)
    assert abs(hd.angle_diff(turned, 130.0)) < 1.0


def test_offset_wraps_through_north():
    a = hd.CompassAnchor(tau_s=1.0)
    for _ in range(100):
        a.learn(mag_deg=350.0, gps_deg=10.0, dt=0.1)
    assert abs(a.offset_deg - 20.0) < 0.1
    assert abs(hd.angle_diff(a.apply(355.0), 15.0)) < 0.1


def test_without_learning_compass_passes_through():
    a = hd.CompassAnchor()
    assert a.offset_deg is None and a.apply(123.0) == 123.0
