"""Звук задней камеры: AAC в живом видео (TS читается, пропуски — тишиной), звук из rtsp_pull (PCM, AAC по запросу),
громкие звуки на охране (клипы WAV, щелчки и время без охраны — нет), маршруты /sound."""

from __future__ import annotations

import io
import itertools
import math
import wave
from fractions import Fraction
from pathlib import Path

import httpx
import numpy as np
import pytest

from fnav.camera import rtsp_pull as rp
from fnav.camera import sound as snd
from fnav.camera.live import AUDIO_LAG_S, LiveMux
from fnav.camera.server import create_app
from fnav.camera.service import AAC_FRAME_PTS, CameraService
from fnav.camera.tsmux import PID_AUDIO, TS, TsMuxer


def _pids(data: bytes) -> list[int]:
    return [((data[i + 1] & 0x1F) << 8) | data[i + 2] for i in range(0, len(data), TS)]


def _aac_frames(n: int, amp: float = 0.2) -> list[bytes]:
    """n кадров AAC (ADTS) с тоном — как их шлёт rtsp_pull."""
    import av

    enc = rp.aac_encoder()
    out: list[bytes] = []
    t = 0
    while len(out) < n:
        x = (np.sin(2 * math.pi * 440 * (np.arange(t, t + rp.AAC_FRAME) / rp.AAC_RATE)) * amp).astype(np.float32)
        f = av.AudioFrame.from_ndarray(x[None, :], format="fltp", layout="mono")
        f.sample_rate, f.pts, f.time_base = rp.AAC_RATE, t, Fraction(1, rp.AAC_RATE)
        t += rp.AAC_FRAME
        out += [rp.adts(bytes(p)) for p in enc.encode(f) if p.pts is None or p.pts >= 0]
    return out[:n]


def _demux(data: bytes, tmp: Path) -> tuple[list[int], list[int], int]:
    import av

    p = tmp / "live.ts"
    p.write_bytes(data)
    with av.open(str(p)) as c:
        assert {s.type for s in c.streams} == {"video", "audio"}
        assert c.streams.audio[0].codec_context.name == "aac"
        v, a, decoded = [], [], 0
        for pkt in c.demux():
            if pkt.pts is None:
                continue
            (v if pkt.stream.type == "video" else a).append(pkt.pts)
            if pkt.stream.type == "audio":
                decoded += sum(fr.samples for fr in pkt.decode())
    return v, a, decoded


def test_ts_muxer_carries_aac_next_to_h264():
    m = TsMuxer(audio=True)
    tables = m.tables()
    assert len(tables) == 2 * TS and bytes([0x0F, 0xE1, 0x01]) in tables  # PMT: AAC на PID 0x101
    assert bytes([0x0F, 0xE1, 0x01]) not in TsMuxer().tables()  # записи — как были, без звука
    frame = rp.adts(bytes(300))
    data = m.mux_audio(frame, 126000)
    assert len(data) % TS == 0 and set(_pids(data)) == {PID_AUDIO}
    assert data[4:10] == b"\x00\x00\x01\xc0" + (8 + len(frame)).to_bytes(2, "big")  # PES звука, длина известна


def test_live_mux_with_audio_is_playable_and_fills_gaps_with_silence(frames_10s, tmp_path):
    mux = LiveMux(15, audio=False)
    mux.new_session(15, audio=True)
    aac = _aac_frames(40)
    mux.set_silence(rp.silent_aac(), AAC_FRAME_PTS)
    assert mux.push_audio(aac[0], frames_10s[0][2], AAC_FRAME_PTS) is None  # до первого ключевого кадра — нет
    out: list[bytes] = []
    a_us = frames_10s[0][2]
    ai = 0
    for au, key, ts in frames_10s[:60]:  # 4 с видео; звук — только первые ~2,5 с, потом «пропал»
        r = mux.push(au, key, ts)
        if r is not None:
            out.append(r[0])
        while ai < len(aac) and a_us <= ts:
            ra = mux.push_audio(aac[ai], a_us, AAC_FRAME_PTS)
            if ra is not None:
                out.append(ra)
            ai += 1
            a_us += rp.AAC_FRAME * 1_000_000 // rp.AAC_RATE
    v, a, samples = _demux(b"".join(out), tmp_path)
    assert len(v) == 60 and samples > 0
    assert all(y > x for x, y in itertools.pairwise(a))  # метки звука только растут
    # звук пропал — дальше тишина: звук не отстаёт от видео больше чем на AUDIO_LAG_S (+ кадр)
    assert (v[-1] - a[-1]) / 90000 <= AUDIO_LAG_S + AAC_FRAME_PTS / 90000 + 0.01
    assert len(a) > ai  # тишины добавлено сверх настоящих кадров


def _av_file(path: Path, seconds: float = 3.0) -> Path:
    """H.264 + G.711 µ-law (как камера) в MOV: тихо, с 1 до 1,5 с — громкий тон."""
    import av

    with av.open(str(path), "w", format="mov") as out:
        vs = out.add_stream("libx264", rate=15)
        vs.width, vs.height, vs.pix_fmt = 160, 90, "yuv420p"
        vs.codec_context.gop_size = 15
        vs.codec_context.max_b_frames = 0
        aus = out.add_stream("pcm_mulaw", rate=8000)
        aus.layout = "mono"
        t_a = 0
        for i in range(int(seconds * 15)):
            vf = av.VideoFrame.from_ndarray(np.full((90, 160, 3), i * 5 % 255, np.uint8), format="rgb24")
            vf = vf.reformat(format="yuv420p")
            vf.pts = i
            for pkt in vs.encode(vf):
                out.mux(pkt)
            n = 8000 // 15
            tt = np.arange(t_a, t_a + n) / 8000.0
            amp = 0.5 if 1.0 <= tt[0] < 1.5 else 0.01
            x = (np.sin(2 * math.pi * 440 * tt) * amp * 32767).astype(np.int16)[None, :]
            af = av.AudioFrame.from_ndarray(x, format="s16", layout="mono")
            af.sample_rate, af.pts = 8000, t_a
            t_a += n
            for pkt in aus.encode(af):
                out.mux(pkt)
        for st in (vs, aus):
            for pkt in st.encode(None):
                out.mux(pkt)
    return path


def _messages(video: Path, **kw) -> list[tuple[int, int, int, bytes]]:
    buf = io.BytesIO()
    with pytest.raises(RuntimeError, match="stream ended"):
        rp.run(str(video), rp.Writer(buf), kw.pop("ctl"), lores_w=160, **kw)
    data, pos, out = buf.getvalue(), 0, []
    while pos < len(data):
        typ, flags, size, ts = rp.HEADER.unpack_from(data, pos)
        out.append((typ, flags, ts, data[pos + rp.HEADER.size:pos + rp.HEADER.size + size]))
        pos += rp.HEADER.size + size
    return out


def test_rtsp_pull_gives_pcm_and_aac_on_request(tmp_path):
    import json

    video = _av_file(tmp_path / "cam.mov")
    msgs = _messages(video, ctl=rp.Control(0, aac=True), audio=True)
    info = json.loads(next(p for t, _, _, p in msgs if t == rp.T_INFO))
    assert info["audio"] == {"codec": "pcm_mulaw", "rate": 8000}
    pcm = b"".join(p for t, _, _, p in msgs if t == rp.T_AUDIO)
    assert abs(len(pcm) / 2 / 8000 - 3.0) < 0.1  # весь звук, PCM s16 8 кГц
    x = np.frombuffer(pcm, "<i2").astype(np.float32)
    loud, quiet = x[int(1.1 * 8000):int(1.4 * 8000)], x[int(2.0 * 8000):int(2.5 * 8000)]
    assert np.sqrt(np.mean(loud ** 2)) > 20 * np.sqrt(np.mean(quiet ** 2))
    aac = [(f, ts, p) for t, f, ts, p in msgs if t == rp.T_AAC]
    assert aac[0][0] == rp.F_SILENT and aac[0][2][:2] == b"\xff\xf1"  # сначала кадр тишины
    assert len(aac) > 30 and all(ts >= 0 for f, ts, _ in aac[1:])
    assert {t for t, *_ in msgs} >= {rp.T_VIDEO, rp.T_AUDIO, rp.T_AAC}
    plain = _messages(video, ctl=rp.Control(0), audio=False)  # без --audio — как было
    assert {t for t, *_ in plain} == {rp.T_INFO, rp.T_VIDEO} and json.loads(plain[0][3])["audio"] is None
    no_aac = _messages(video, ctl=rp.Control(0), audio=True)  # живое видео не смотрят — AAC не кодируется
    assert rp.T_AAC not in {t for t, *_ in no_aac}


class _Clock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.base = 1_793_550_000.0 - 1000.0  # «сейчас» — недавно: старые клипы не удалятся по возрасту

    def wall(self) -> float:
        return self.base + self.mono


def _feed(m: snd.SoundMonitor, clk: _Clock, t: list[int], seconds: float, amp: float, rng) -> None:
    for _ in range(0, int(seconds * 8000), 400):
        m.feed(rng.normal(0, amp, 400).astype(np.int16).tobytes(), t[0])
        t[0] += 400 * 1_000_000 // 8000
        clk.mono = t[0] / 1e6


def test_sound_monitor_clips_loud_sounds_only_while_armed(tmp_path):
    clk, t, rng = _Clock(), [1_000_000_000], np.random.default_rng(1)
    m = snd.SoundMonitor(tmp_path, wall=clk.wall, mono=lambda: clk.mono)
    _feed(m, clk, t, 10, 40, rng)  # фон
    _feed(m, clk, t, 1, 6000, rng)  # громко, но охрана снята — ни клипа, ни события
    _feed(m, clk, t, 5, 40, rng)
    assert m.events_after(0) == [] and list(tmp_path.iterdir()) == []
    assert -62 < m.floor_db < -54 and m.level_db is not None
    m.armed = True
    _feed(m, clk, t, 0.05, 8000, rng)  # щелчок короче MIN_LOUD_S — выброшен
    _feed(m, clk, t, 5, 40, rng)
    _feed(m, clk, t, 1.0, 6000, rng)  # громкий звук 1 с
    _feed(m, clk, t, 5, 40, rng)
    (ev,) = m.events_after(0)
    assert ev["seq"] == 1 and ev["dur_s"] == pytest.approx(1.1, abs=0.15) and ev["peak_db"] > -20
    assert ev["clip_start"] == pytest.approx(ev["start"] - snd.PRE_S, abs=0.11)  # за 5 с до звука
    clip = tmp_path / ev["clip"]
    assert snd.CLIP_RE.match(ev["clip"]) and sorted(p.name for p in tmp_path.iterdir()) == [ev["clip"]]
    with wave.open(str(clip)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 8000)
        assert w.getnframes() / 8000 == pytest.approx(ev["clip_dur_s"], abs=0.05)
    _feed(m, clk, t, 0.5, 6000, rng)  # охрану сняли посреди звука — что успели, то отметка
    m.armed = False
    assert [e["seq"] for e in m.events_after(1)] == [2] and m.events_after(2) == []
    assert not list(tmp_path.glob("*.part"))


def test_sound_clips_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(snd, "CLIPS_MAX", 2)
    clk, t, rng = _Clock(), [1_000_000_000], np.random.default_rng(2)
    m = snd.SoundMonitor(tmp_path, wall=clk.wall, mono=lambda: clk.mono)
    m.armed = True
    _feed(m, clk, t, 5, 40, rng)
    for _ in range(3):
        _feed(m, clk, t, 0.5, 6000, rng)
        _feed(m, clk, t, 4, 40, rng)
    names = [e["clip"] for e in m.events_after(0)]
    assert len(names) == 3 and sorted(p.name for p in tmp_path.iterdir()) == names[1:]  # самый старый удалён
    (tmp_path / "x.wav.part").write_bytes(b"")
    snd.SoundMonitor(tmp_path)  # оборванные клипы — при запуске
    assert not (tmp_path / "x.wav.part").exists()


async def test_sound_routes(tmp_path):
    svc = CameraService(root=tmp_path / "rec", settings_path=tmp_path / "s.json", rtsp_url=None, camera_id="rear",
                        sound_dir=tmp_path / "clips")
    front = CameraService(root=tmp_path / "rec2", settings_path=tmp_path / "s.json", rtsp_url=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(svc, manage_service=False)),
                                 base_url="http://cam") as c:
        assert (await c.post("/sound", json={"armed": "yes"})).status_code == 400
        assert (await c.post("/sound", json={"armed": True})).json() == {"ok": True, "armed": True}
        assert svc.sound.armed is True
        assert (await c.get("/sound/events", params={"after": 0})).json() == {"events": [], "seq": 0}
        assert (await c.get("/status")).json()["audio"] is None  # звука от камеры ещё не было
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(front, manage_service=False)),
                                 base_url="http://cam") as c:
        assert (await c.post("/sound", json={"armed": True})).status_code == 409  # у передней микрофона нет
