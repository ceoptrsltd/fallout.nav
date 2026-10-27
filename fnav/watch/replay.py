"""Офлайн-прогон записи через тот же конвейер: python -m fnav.watch.replay <file.ts> [--near-line 0.6] [--out dir]

Декодирует .ts (PyAV), берёт ~4 кадра/с по PTS, уменьшает до lores (768×576, как кольцо fnav-camera) и
прогоняет через Watcher. Печатает события со временем от начала файла, в --out пишет размеченные JPEG: каждое
событие и (не чаще раза в секунду) кадры с найденными людьми/лицами. --sim-ms N: пропускать кадры, пришедшие,
пока нейросеть работала N мс (как на CM4, где NanoDet ~0,3 с).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from fnav.models import Settings, VisionSettings

from .frame import Frame
from .pipeline import Watcher


def iter_frames(path: Path, fps: float, w: int, h: int, start: float = 0.0, duration: float | None = None):
    """(t от начала, I420 (h*3/2, w)) с шагом ~1/fps по PTS."""
    import av

    with av.open(str(path)) as c:
        s = c.streams.video[0]
        s.thread_type = "AUTO"
        t0 = None
        next_t = start
        for fr in c.decode(s):
            if fr.pts is None:
                continue
            ts = float(fr.pts * fr.time_base)
            if t0 is None:
                t0 = ts
            t = ts - t0
            if duration is not None and t > start + duration:
                break
            if t + 1e-3 < next_t:
                continue
            next_t = max(next_t + 1 / fps, t + 0.5 / fps)
            yield t, fr.to_ndarray(width=w, height=h, format="yuv420p")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fnav.watch.replay", description="run fnav-watch over a recording")
    ap.add_argument("file", type=Path, help="recorded .ts (or any video PyAV can decode)")
    ap.add_argument("--near-line", type=float, default=None, help="near line, fraction of height from the top")
    ap.add_argument("--face-min", type=float, default=None)
    ap.add_argument("--approach-hold", type=float, default=None, help="approach_hold_s")
    ap.add_argument("--face-hold", type=float, default=None, help="face_hold_s")
    ap.add_argument("--loiter", type=float, default=None, help="loiter_s")
    ap.add_argument("--clock", default=None, help="wall clock of the first frame, HH:MM:SS[.s] (prints event times)")
    ap.add_argument("--settings", type=Path, default=None, help="settings.json to take alarm.vision from")
    ap.add_argument("--fps", type=float, default=4.0)
    ap.add_argument("--size", default="768x576", help="lores size WxH")
    ap.add_argument("--start", type=float, default=0.0, help="skip seconds")
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--sim-ms", type=float, default=0.0, help="simulate person inference time (CM4: ~300)")
    ap.add_argument("--models", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None, help="write annotated JPEGs here")
    ap.add_argument("--every", type=float, default=1.0, help="min seconds between saved detection frames")
    ap.add_argument("--all", action="store_true", help="save every processed frame")
    args = ap.parse_args(argv)

    vs = VisionSettings()
    if args.settings:
        vs = Settings.model_validate_json(args.settings.read_text(encoding="utf-8")).alarm.vision
    upd = {k: v for k, v in (("near_line", args.near_line), ("face_min", args.face_min),
                             ("approach_hold_s", args.approach_hold), ("face_hold_s", args.face_hold),
                             ("loiter_s", args.loiter)) if v is not None}
    vs = vs.model_copy(update=upd)
    w, h = (int(v) for v in args.size.lower().split("x"))

    from .detect import load_detectors

    person, face = load_detectors(args.models)
    watcher = Watcher(person, face, vs)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    print(f"{args.file.name}: near_line {vs.near_line}, approach_hold {vs.approach_hold_s} s, "
          f"face_min {vs.face_min}, face_hold {vs.face_hold_s} s, loiter {vs.loiter_s} s", flush=True)
    busy_until = -1.0
    last_saved = -1e9
    n_events = 0
    t_start = time.perf_counter()
    t = 0.0
    for t, i420 in iter_frames(args.file, args.fps, w, h, args.start, args.duration):
        if t < busy_until:
            continue
        frame = Frame(t, "yuv420", i420, w, h, wall=t)
        runs = watcher.stats.person_runs
        events = watcher.process(frame)
        if args.sim_ms and watcher.stats.person_runs != runs:
            busy_until = t + args.sim_ms / 1000
        snap = watcher.snap
        for ev in events:
            n_events += 1
            print(f"{_mmss(t)}{_clock(args.clock, t)}  EVENT {ev.kind:8s} {json.dumps(ev.data)}", flush=True)
            if args.out:
                (args.out / f"event_{n_events:02d}_{ev.kind}_{t:07.1f}.jpg").write_bytes(watcher.render() or b"")
        found = snap.dets or snap.faces
        if args.out and (args.all or (found and t - last_saved >= args.every)):
            last_saved = t
            (args.out / f"f_{t:07.1f}.jpg").write_bytes(watcher.render() or b"")
    st = watcher.stats
    wall = time.perf_counter() - t_start
    print(f"done: {st.frames} frames ({t:.0f} s of video) in {wall:.1f} s; motion in {st.motion_frames}; "
          f"person runs {st.person_runs} (~{st.person_ms:.0f} ms), face runs {st.face_runs} (~{st.face_ms:.0f} ms), "
          f"gate ~{st.gate_ms:.2f} ms; events {n_events}")
    return 0


def _mmss(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def _clock(start: str | None, t: float) -> str:
    """«  22:05:31» — время события по часам, если задано время первого кадра."""
    if not start:
        return ""
    h, m, s = (float(v) for v in start.split(":"))
    x = (h * 3600 + m * 60 + s + t) % 86400
    return f"  {int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{x % 60:04.1f}"


if __name__ == "__main__":
    sys.exit(main())
