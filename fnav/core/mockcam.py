"""Встроенная mock-камера для разработки на ПК: MJPEG 768×576 ~10 fps (Pillow)."""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from fnav.models import CameraStatus

log = logging.getLogger("fnav.core.mockcam")

W, H = 768, 576
FPS = 10.0
BOUNDARY = "frame"


def _font(size: int):
    from PIL import ImageFont

    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


class MockCamera:
    def __init__(self, recordings_root: Path):
        self.recordings_root = recordings_root
        self._frame: bytes = b""
        self._seq = 0
        self._cond = asyncio.Condition()
        self._task: asyncio.Task | None = None
        self._viewers = 0
        self._idle_since = time.monotonic()
        self._base = None
        self._fonts = None
        self.started_utc = datetime.now(UTC).isoformat(timespec="seconds")
        self.current_segment: str | None = None

    # ── картинка ──
    def _render(self, n: int) -> bytes:
        from PIL import Image, ImageDraw

        if self._base is None:
            base = Image.new("RGB", (W, H), (20, 22, 20))
            d = ImageDraw.Draw(base)
            bars = [(192, 192, 192), (192, 192, 0), (0, 192, 192), (0, 192, 0),
                    (192, 0, 192), (192, 0, 0), (0, 0, 192), (16, 16, 16)]
            bw = W / len(bars)
            for i, c in enumerate(bars):
                d.rectangle([int(i * bw), 0, int((i + 1) * bw), int(H * 0.22)], fill=c)
            for x in range(0, W, 48):
                d.line([(x, 0), (x, H)], fill=(55, 60, 55))
            for y in range(0, H, 48):
                d.line([(0, y), (W, y)], fill=(55, 60, 55))
            d.ellipse([W / 2 - 200, H / 2 - 200, W / 2 + 200, H / 2 + 200], outline=(230, 230, 230), width=3)
            d.line([(W / 2, H / 2 - 210), (W / 2, H / 2 + 210)], fill=(230, 230, 230), width=1)
            d.line([(W / 2 - 210, H / 2), (W / 2 + 210, H / 2)], fill=(230, 230, 230), width=1)
            self._base = base
            self._fonts = (_font(64), _font(40), _font(22))
        img = self._base.copy()
        d = ImageDraw.Draw(img)
        big, mid, small = self._fonts
        # вращающаяся стрелка — видно, что поток живой
        a = n / FPS * 1.2
        cx, cy = W / 2, H / 2
        d.line([(cx, cy), (cx + 190 * math.sin(a), cy - 190 * math.cos(a))], fill=(255, 210, 60), width=6)
        txt = "MOCK CAMERA"
        tb = d.textbbox((0, 0), txt, font=big)
        tx = (W - (tb[2] - tb[0])) / 2
        d.rectangle([tx - 16, H * 0.26 - 8, tx + (tb[2] - tb[0]) + 16, H * 0.26 + (tb[3] - tb[1]) + 22],
                    fill=(0, 0, 0))
        d.text((tx, H * 0.26), txt, font=big, fill=(255, 255, 255))
        now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S.") + f"{int(time.time() * 10) % 10}"
        nb = d.textbbox((0, 0), now, font=mid)
        nx = (W - (nb[2] - nb[0])) / 2
        d.rectangle([nx - 12, H - 110, nx + (nb[2] - nb[0]) + 12, H - 50], fill=(0, 0, 0))
        d.text((nx, H - 104), now, font=mid, fill=(255, 255, 255))
        d.text((20, H - 36), f"768x576  frame {n}", font=small, fill=(220, 220, 220))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=75)
        return buf.getvalue()

    async def _producer(self) -> None:
        n = 0
        period = 1.0 / FPS
        nxt = time.monotonic()
        try:
            while True:
                if self._viewers == 0 and time.monotonic() - self._idle_since > 5:
                    break
                try:
                    frame = await asyncio.to_thread(self._render, n)
                except Exception:
                    log.exception("mock camera: render error")
                    await asyncio.sleep(1)
                    continue
                n += 1
                async with self._cond:
                    self._frame = frame
                    self._seq += 1
                    self._cond.notify_all()
                nxt += period
                delay = nxt - time.monotonic()
                if delay < -1:
                    nxt = time.monotonic()
                    delay = 0
                await asyncio.sleep(max(0.0, delay))
        finally:
            self._task = None

    def _ensure_producer(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._producer(), name="mockcam")

    async def snapshot(self) -> bytes:
        if not self._frame or self._task is None or self._task.done():
            return await asyncio.to_thread(self._render, 0)  # поток не идёт — свежий кадр
        return self._frame

    async def frames(self) -> AsyncIterator[bytes]:
        self._viewers += 1
        self._ensure_producer()
        last = [0]  # seq 0 = кадров ещё нет; иначе сразу отдаём текущий
        try:
            while True:
                async with self._cond:
                    await self._cond.wait_for(lambda: self._seq != last[0])
                    frame, last[0] = self._frame, self._seq
                yield frame
                self._ensure_producer()
        finally:
            self._viewers -= 1
            self._idle_since = time.monotonic()

    async def mjpeg(self) -> AsyncIterator[bytes]:
        async for frame in self.frames():
            yield (
                f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n".encode()
                + frame + b"\r\n"
            )

    # ── статус и «записи» ──
    def status(self) -> CameraStatus:
        return CameraStatus(
            state="recording",
            recording=True,
            current_segment=self.current_segment,
            segment_started_utc=self.started_utc,
            fps_actual=15.0,
            bitrate_kbps=6000.0,
            sensor_mode="2592x1944",
            output_size="1440x1080",
            time_synced=True,
        )

    def ensure_demo_recordings(self) -> None:
        """Если записей нет — создать несколько маленьких демонстрационных .ts (только mock)."""
        root = self.recordings_root
        try:
            if root.is_dir() and any(root.rglob("*.ts")):
                newest = max(root.rglob("*.ts"), key=lambda p: p.stat().st_mtime)
                self.current_segment = newest.relative_to(root).as_posix()
                return
            now = time.time()
            names = []
            # первая — «вчера», остальные — сегодня с шагом 10 минут, последняя — «текущая»
            for i, mins in enumerate((24 * 60 + 40, 30, 20, 10, 0)):
                t = datetime.fromtimestamp(now - mins * 60).replace(microsecond=0)
                p = root / t.strftime("%Y-%m-%d") / f"{t.strftime('%Y-%m-%d_%H-%M-%S')}.ts"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(_demo_ts(seconds=3 + i))
                end = min(t.timestamp() + 595, now - 30) if mins else now
                os.utime(p, (end, end))
                names.append(p)
            (names[1].with_name(names[1].name + ".lock")).touch()
            self.current_segment = names[-1].relative_to(root).as_posix()
            log.info("mock: created demo recordings in %s", root)
        except OSError as e:
            log.warning("mock: cannot create demo recordings: %s", e)

    def protect_current(self) -> dict:
        seg = self.current_segment
        if seg:
            p = self.recordings_root / seg
            if p.exists():
                p.with_name(p.name + ".lock").touch()
        return {"ok": True, "segment": seg, "mock": True}


def _demo_ts(seconds: int) -> bytes:
    """MPEG-TS из пакетов с одним PES/PTS в секунду (видео PID 0x100) — для проверки длительности."""
    out = bytearray()
    for s in range(seconds + 1):
        pts = s * 90000
        pes = bytearray(b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05")
        pes += bytes([
            0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, 0x01 | ((pts >> 14) & 0xFE),
            (pts >> 7) & 0xFF, 0x01 | ((pts << 1) & 0xFE),
        ])
        pkt = bytearray(b"\x47\x41\x00\x10") + pes
        pkt += b"\xff" * (188 - len(pkt))
        out += pkt
        for _ in range(20):  # «полезная нагрузка» — null-пакеты
            out += b"\x47\x1f\xff\x10" + b"\xff" * 184
    return bytes(out)
