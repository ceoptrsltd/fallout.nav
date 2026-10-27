"""Детектор движения на синтетических кадрах: шум (день/ночь), объект, скачок экспозиции, свет, метка времени."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("cv2")

from watchhelpers import H, W, scene, with_box

from fnav.watch.motion import MotionGate


def run(gate, frames, t0=0.0, dt=0.25, exp=None):
    out = []
    for i, y in enumerate(frames):
        out.append(gate.update(y, t0 + i * dt, exp[i] if exp else None))
    return out


@pytest.mark.parametrize("noise", [2.0, 12.0])  # день; ночь, усиление 16×
def test_static_noise_is_not_motion(noise):
    rng = np.random.default_rng(1)
    gate = MotionGate(bottom=0.7)
    res = run(gate, [scene(rng, noise) for _ in range(40)])
    assert not any(m.moving for m in res)
    assert res[-1].thr >= 6.0


def test_moving_object_is_motion():
    rng = np.random.default_rng(2)
    gate = MotionGate(bottom=0.7)
    frames = [scene(rng) for _ in range(12)]
    for i in range(8):  # человек идёт слева направо
        x = 0.2 + 0.05 * i
        frames.append(with_box(scene(rng), (x, 0.25, x + 0.08, 0.62), 220))
    res = run(gate, frames)
    assert not any(m.moving for m in res[:12])
    assert all(m.moving for m in res[13:])  # 2 из 3 кадров
    b = res[-1].blobs[0]
    assert b[0] < 0.6 < b[2] and b[1] < 0.3


def test_motion_at_night_noise():
    rng = np.random.default_rng(3)
    gate = MotionGate(bottom=0.7)
    frames = [scene(rng, 12.0) for _ in range(12)]
    frames += [with_box(scene(rng, 12.0), (0.4 + 0.03 * i, 0.3, 0.5 + 0.03 * i, 0.65), 200) for i in range(6)]
    res = run(gate, frames)
    assert not any(m.moving for m in res[:12])
    assert res[-1].moving


def test_exposure_jump_resets_and_is_quiet():
    rng = np.random.default_rng(4)
    gate = MotionGate(bottom=0.7)
    base = scene(rng, 0).astype(np.float32)
    frames = [scene(rng, 2, base) for _ in range(8)]
    exp = [66000.0 * 8] * 8
    # фары: AE вдвое уменьшила выдержку, картинка немного другая
    brighter = base * 0.85 + 20
    frames += [scene(rng, 2, brighter) for _ in range(12)]
    exp += [33000.0 * 8] * 12
    res = run(gate, frames, exp=exp)
    assert res[8].reset
    assert not any(m.moving for m in res)


def test_global_light_change_resets():
    rng = np.random.default_rng(5)
    gate = MotionGate(bottom=0.7)
    base = scene(rng, 0).astype(np.float32)
    frames = [scene(rng, 2, base) for _ in range(8)] + [scene(rng, 2, base + 45) for _ in range(10)]
    res = run(gate, frames)  # экспозиции нет в метаданных — по доле изменившихся пикселей
    assert any(m.reset for m in res[8:10])
    assert not any(m.moving for m in res)


def test_timestamp_and_hood_are_ignored():
    rng = np.random.default_rng(6)
    gate = MotionGate(bottom=0.7)
    frames = []
    for i in range(20):
        y = scene(rng)
        y[6:40, 6:200] = 235 if i % 2 else 16  # метка времени меняется
        y[int(0.75 * H):, int(0.3 * W):int(0.7 * W)] = 40 + 10 * i  # блики на капоте
        frames.append(y)
    assert not any(m.moving for m in run(gate, frames))


def test_gate_cost_is_small():
    import time

    rng = np.random.default_rng(7)
    gate = MotionGate(bottom=0.7)
    frames = [scene(rng) for _ in range(10)]
    run(gate, frames[:3])
    t0 = time.perf_counter()
    for i in range(50):
        gate.update(frames[i % 10], 10 + i * 0.25)
    ms = (time.perf_counter() - t0) / 50 * 1e3
    assert ms < 10, f"gate {ms:.2f} ms per frame"
