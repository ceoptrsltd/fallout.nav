"""Заряд свинцового аккумулятора 12 V только по напряжению — третья станция (VictronStation, fnav/core/power.py).
docs/contracts/power.md «Заряд свинцового: только по напряжению».

Есть лишь напряжение батареи (0.01 В, примерно раз в минуту, бывают дыры по несколько минут) и состояние солнечного
контроллера. Шунта нет: тока нагрузки и тока чужой зарядки (сеть, DC-DC, генератор) не знаем. Поэтому:

- Заряд, % — только по напряжению покоя (leadbatt.soc_from_voltage), и только когда батарея правда в покое: зарядки
  нет, после последней зарядки прошло SETTLE_S (поверхностный заряд стёк), напряжение ровное STABLE_S (нагрузка не
  меняется). Такое значение — «доверенное»: держим его, пока не будет нового, и храним в файле (переживает
  перезапуск). Покой продолжается — значение обновляется с каждым показанием.
- Зарядку чужим источником видим по самому напряжению (_detect_external): выше любого покоя (CHARGE_V) и растёт
  понемногу, но устойчиво (не одной ступенькой, как при выключении нагрузки), или держится высоко и ровно
  (абсорбция/поддержка зарядного устройства).
- «Полная» — без покоя: контроллер Victron перешёл в float/storage (сам решил, что абсорбция кончилась) или
  напряжение абсорбции держалось FULL_ABSORB_S.
- Чего так не узнать: сколько набралось во время зарядки (держим значение до неё, с пометкой «примерно»), расход под
  нагрузкой между покоями, ёмкость и износ батареи. Температура не учтена (датчика у батареи нет).
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

from fnav.core.config import atomic_write_text
from fnav.core.leadbatt import soc_from_voltage

log = logging.getLogger("fnav.core.leadsoc")

SAMPLE_GAP_S = 20.0  # пакеты одного окна приёма (advscan, 1–2 с) — одно показание: оставляем последний
GAP_RESET_S = 420.0  # дольше нет показаний (как VICTRON_STALE_S) — тренд через дыру не считаем, копим заново
HISTORY_S = 1800.0
# Зарядка чужим источником. Покой полной батареи — 12.7–12.9 В (AGM/гель до ~12.95); выше — зарядка или стекающий
# поверхностный заряд. Поверхностный заряд только падает, зарядка — держит или поднимает.
CHARGE_V = 13.0
CHARGE_OFF_V = 12.95  # гистерезис: зарядное ниже покоя не держит
RISE_WINDOW_S = 300.0  # рост ищем за 5 мин (≈5 показаний) ...
RISE_MIN_S = 110.0  # ... но не меньше чем за 2 интервала
RISE_V = 0.03  # устойчивый рост: шум 0.01 В (мерцание младшего разряда) столько не даёт
STEP_V = 0.05  # скачок от этого — выключили/включили нагрузку (или само зарядное включилось): в рост не считаем
HOLD_WINDOW_S = 600.0  # держится ровно 10 мин ...
HOLD_V = 13.2  # ... выше этого — абсорбция/поддержка зарядного (поверхностный заряд здесь заметно падает)
FLAT_V = 0.01
# Слабое зарядное поднимает медленно (замер 2026-11-16: 13.03 → 13.05 В за 20 мин) — RISE_V за 5 мин не набирается:
SLOW_WINDOW_S = 1200.0  # 20 мин не ниже CHARGE_V, без падения и с подъёмом на SLOW_RISE_V — тоже зарядка
SLOW_RISE_V = 0.02  # больше мерцания младшего разряда; поверхностный заряд после зарядки только падает
STOP_DROP_V = 0.10  # упало за HOLD_WINDOW_S на столько — зарядное отключилось (просадка от лёгкой нагрузки меньше)
# Покой: заряд по напряжению берём только так
SETTLE_S = 3 * 3600.0  # после зарядки: поверхностный заряд стекает часы (Battery University — от 4 ч, Trojan — 6 ч);
#                        дальше значение всё равно обновляется с каждым ровным показанием и уточняется
SETTLE_HIGH_S = 8 * 3600.0  # покой выше REST_MAX_V бывает только у полной батареи — верим такому после долгой паузы
REST_MAX_V = 12.9
STABLE_S = 900.0  # напряжение ровное 15 мин: нагрузка не менялась (холодильник в паузе, всё выключено)
STABLE_V = 0.02
STABLE_N = 3
# Полная без покоя
FULL_STATES = frozenset({"float", "storage"})  # Victron: абсорбция кончилась (по току хвоста или по времени)
ABSORB_V = 14.0  # абсорбция 14.4 В при 25 °C с термокомпенсацией −18 мВ/°C: и в жару выше 14.0
FULL_ABSORB_S = 2 * 3600.0
# Показ
FRESH_S = 3600.0  # доверенному значению больше часа — уже «примерно» (батарея могла работать)
TRUST_MAX_S = 12 * 3600.0  # старше — в покое лучше оценка по напряжению сейчас (тоже «примерно»), чем такое значение
KEEP_S = 2 * 86400.0  # из файла: старше двух суток не берём
SAVE_S = 600.0  # в файл не чаще (карта памяти)


class LeadSoc:
    """Оценка заряда и зарядки чужим источником по напряжению. Часы — mono (интервалы) и wall (файл)."""

    def __init__(self, mono: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 path: Path | None = None) -> None:
        self.mono, self.wall, self.path = mono, wall, path
        self.hist: deque[tuple[float, float]] = deque()
        self.external = False  # заряжает источник, которого контроллер не видит
        self.solar = False
        self.charge_t: float | None = None  # когда последний раз видели зарядку (любую)
        self.soc: float | None = None  # доверенное значение, %
        self.soc_t: float | None = None
        self.full = False  # доверенное — «полная» по зарядному, а не по покою
        self.absorb_t: float | None = None
        self.started = mono()
        self._saved_t: float | None = None
        self._dirty = False
        self._load()

    # ── показания ──
    def reset_trend(self) -> None:
        self.hist.clear()
        self.external = False

    def feed(self, v: float, solar: bool, state: str = "") -> None:
        """Показание: напряжение батареи, солнечный контроллер заряжает, его состояние (victron.STATES)."""
        now = self.mono()
        if self.hist and now - self.hist[-1][0] > GAP_RESET_S:
            self.hist.clear()
        if self.hist and now - self.hist[-1][0] < SAMPLE_GAP_S:
            self.hist[-1] = (self.hist[-1][0], v)  # время — начала окна: и сплошной поток даёт точку раз в 20 с
        else:
            self.hist.append((now, v))
        while self.hist and self.hist[0][0] < now - HISTORY_S:
            self.hist.popleft()
        was = self.charging
        self.solar = solar
        self._detect_external(now, v)
        if self.charging:
            if not was:
                self.absorb_t = None
                self._dirty = True
            self.charge_t = now
            self._absorption(now, v)
        elif was:
            self._save(force=True)  # конец зарядки: после перезапуска знать, что батарея ещё «отходит»
        if state in FULL_STATES:
            self._set_full(now, f"charger {state}")
        elif self.full and self.charging and v >= HOLD_V:
            self.soc_t = now  # зарядное всё ещё держит полную
        if self._at_rest(now, v):
            vs = [x for t, x in self.hist if t >= now - STABLE_S]
            soc = soc_from_voltage(sum(vs) / len(vs))
            if self.soc is None or abs(soc - self.soc) >= 2 or self.full:  # в журнал и файл — заметные перемены
                log.info("lead battery: %.0f %% at rest (%.2f V)", soc, v)
                self._dirty = True
            self.soc, self.soc_t, self.full = soc, now, False
        self._save()

    @property
    def charging(self) -> bool:
        return self.solar or self.external

    def _window(self, now: float, span: float) -> list[tuple[float, float]]:
        return [(t, x) for t, x in self.hist if t >= now - span]

    def _detect_external(self, now: float, v: float) -> None:
        if self.external:
            top = max(x for _t, x in self._window(now, HOLD_WINDOW_S))
            if v < CHARGE_OFF_V or round(top - v, 2) >= STOP_DROP_V:
                self.external = False
                log.info("lead battery: charging from another source ended (%.2f V)", v)
            return
        if v >= CHARGE_V and (self._rising(now) or self._holding(now, HOLD_WINDOW_S, HOLD_V)
                              or self._holding(now, SLOW_WINDOW_S, CHARGE_V, SLOW_RISE_V)):
            self.external = True
            log.info("lead battery: charging from another source (%.2f V)", v)

    def _rising(self, now: float) -> bool:
        """Устойчивый рост: сумма мелких шагов (скачки от нагрузки не в счёт), не меньше двух шагов вверх, последний
        шаг — не вниз."""
        w = self._window(now, RISE_WINDOW_S)
        if len(w) < 3 or w[-1][0] - w[0][0] < RISE_MIN_S:
            return False
        steps = [round(b[1] - a[1], 2) for a, b in pairwise(w)]
        small = [d for d in steps if abs(d) < STEP_V]
        return round(sum(small), 2) >= RISE_V and sum(d > 0 for d in small) >= 2 and steps[-1] >= 0

    def _holding(self, now: float, span: float, level: float, rise: float = -FLAT_V) -> bool:
        """Не ниже level и без падения всё окно span (и поднялось не меньше чем на rise) — зарядное держит или
        медленно поднимает (поверхностный заряд так не стоит: он падает)."""
        w = self._window(now, span)
        if len(w) < 4 or w[-1][0] - w[0][0] < span - 120:
            return False
        vs = [x for _t, x in w]
        return min(vs) >= level and round(max(vs) - vs[-1], 2) <= FLAT_V and round(vs[-1] - vs[0], 2) >= rise

    def _absorption(self, now: float, v: float) -> None:
        if v >= ABSORB_V:
            self.absorb_t = self.absorb_t if self.absorb_t is not None else now
        elif v < HOLD_V:
            self.absorb_t = None
        if self.absorb_t is not None and now - self.absorb_t >= FULL_ABSORB_S:
            self._set_full(now, "absorption held 2 h")

    def _set_full(self, now: float, why: str) -> None:
        if not self.full or self.soc != 100.0:
            log.info("lead battery: full (%s)", why)
            self._dirty = True
        self.soc, self.soc_t, self.full = 100.0, now, True

    def settling(self, now: float | None = None) -> bool:
        """После зарядки ещё не прошло SETTLE_S: напряжение завышено поверхностным зарядом."""
        now = self.mono() if now is None else now
        return self.charge_t is not None and now - self.charge_t < SETTLE_S

    def _at_rest(self, now: float, v: float) -> bool:
        if self.charging or self.settling(now):
            return False
        w = self._window(now, STABLE_S)
        if len(w) < STABLE_N or w[-1][0] - w[0][0] < STABLE_S - 120:
            return False
        vs = [x for _t, x in w]
        if round(max(vs) - min(vs), 2) > STABLE_V:
            return False
        if v > REST_MAX_V:  # зарядки не видели с запуска — считаем от запуска
            since = now - (self.charge_t if self.charge_t is not None else self.started)
            return since >= SETTLE_HIGH_S
        return True

    # ── наружу ──
    def view(self, v: float) -> tuple[float | None, bool]:
        """(заряд, % или None — прочерк; True — «примерно»). v — напряжение сейчас."""
        now = self.mono()
        busy = self.charging or self.settling(now)
        if self.soc is not None and self.soc_t is not None:
            age = now - self.soc_t
            if busy or age <= TRUST_MAX_S:
                return self.soc, age > FRESH_S or (busy and not self.full)
        if busy or v >= CHARGE_V:  # выше любого покоя: зарядка, которую ещё не распознали, или поверхностный заряд
            return None, False  # напряжение под зарядкой и сразу после неё о заряде не говорит
        return soc_from_voltage(v), True

    def note(self, v: float) -> str:
        """Откуда процент — для GET /api/power (веб-интерфейс, страница Bluetooth)."""
        now = self.mono()
        soc, _approx = self.view(v)
        src = "solar" if self.solar else "another source" if self.external else ""
        if soc is None:
            return (f"none yet: charging from {src}" if src else "none yet: settling after charging"
                    if self.settling(now) else "none yet: the voltage is above any resting one")
        if self.soc is None or self.soc_t is None or soc != self.soc:
            return "estimate from the voltage now"
        age = round((now - self.soc_t) / 60)
        what = "full (charger)" if self.full else "at rest"
        when = "now" if age <= 2 else f"{age} min ago" if age < 120 else f"{age // 60} h ago"
        tail = f"; charging from {src}" if src else "; settling after charging" if self.settling(now) else ""
        return f"{what}, {when}{tail}"

    # ── файл ──
    def _load(self) -> None:
        if self.path is None:
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            now, wall = self.mono(), self.wall()

            def ok(at: object) -> bool:  # часы ушли назад (время из будущего) или слишком давно — не берём
                return isinstance(at, int | float) and wall - KEEP_S <= at <= wall + 60

            if ok(d.get("soc_at")) and d.get("soc") is not None:
                self.soc, self.soc_t, self.full = float(d["soc"]), now - (wall - float(d["soc_at"])), bool(d.get("full"))
            if ok(d.get("charge_at")):
                self.charge_t = now - (wall - float(d["charge_at"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def _save(self, force: bool = False) -> None:
        """События (новое значение, полная, начало и конец зарядки) — сразу; отметки времени — раз в SAVE_S."""
        if self.path is None or (self.soc_t is None and self.charge_t is None):
            return
        now = self.mono()
        if not (force or self._dirty or self._saved_t is None or now - self._saved_t >= SAVE_S):
            return
        wall = self.wall()
        d = {"soc": self.soc, "soc_at": None if self.soc_t is None else round(wall - (now - self.soc_t)),
             "full": self.full, "charge_at": None if self.charge_t is None else round(wall - (now - self.charge_t))}
        try:
            atomic_write_text(self.path, json.dumps(d))
            self._saved_t, self._dirty = now, False
        except OSError as e:
            log.warning("lead battery: cannot save the charge: %s", e)

    def save(self) -> None:
        self._save(force=True)
