from fnav.models import GpsFix, GpsSettings
from fnav.sensors import ubx
from fnav.sensors.stillness import StillnessFilter


def fix(v, hdop=1.0, course=90.0):
    return GpsFix(fix="3d", lat=52.0, lon=21.0, speed_mps=v, course_deg=course, hdop=hdop, sats_used=8)


def test_jitter_at_standstill_is_zero():
    f, cfg = StillnessFilter(), GpsSettings()
    t = 0.0
    for v in [0.3, 1.2, 0.9, 1.4, 0.2, 1.6, 0.4]:  # шум GPS на стоянке, короткий выброс 1.6
        out = f.apply(t, fix(v), cfg)
        assert out.speed_mps == 0.0 and out.course_deg is None
        t += 0.1


def test_start_moving_needs_confirmation_then_stop_immediately():
    f, cfg = StillnessFilter(), GpsSettings()
    outs = [f.apply(i * 0.1, fix(3.0), cfg).speed_mps for i in range(15)]
    assert outs[0] == 0.0 and outs[9] == 0.0  # < 1 с выше порога — ещё «стоим»
    assert outs[-1] == 3.0  # подтвердилось — едем
    assert f.apply(2.0, fix(1.2), cfg).speed_mps == 1.2  # между порогами — продолжаем ехать
    assert f.apply(2.1, fix(0.5), cfg).speed_mps == 0.0  # ниже 0.8 — сразу стоим


def test_poor_hdop_doubles_thresholds():
    f, cfg = StillnessFilter(), GpsSettings()
    outs = [f.apply(i * 0.1, fix(2.5, hdop=5.0), cfg).speed_mps for i in range(20)]
    assert all(v == 0.0 for v in outs)  # 2.5 < 1.5*2


def test_no_fix_passthrough():
    f, cfg = StillnessFilter(), GpsSettings()
    nf = GpsFix(fix="none")
    assert f.apply(0, nf, cfg) is nf


def test_cfg_nav5_static_hold_payload():
    m = ubx.parse_frame(ubx.cfg_nav5_static_hold(50, 20))
    p = m.payload if hasattr(m, "payload") else m[2]
    assert p[0:2] == bytes([0x40, 0x00]) and p[22] == 50 and p[28:30] == bytes([20, 0])
