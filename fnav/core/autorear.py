"""Задний вид при заднем ходе: экран сам показывает заднюю камеру, пока фургон едет назад.

Вход — с каждой телеметрией fnav-core (10 Гц): `SensorSnapshot.reversing` (хаб датчиков), скорость GPS, включено ли
(`settings.display.auto_rear_on_reverse` и задняя камера) и что показывает экран (`Core.view_camera`). Выход — новый
вид экрана; Core переключает его тем же путём, что кнопка камеры (`view_camera`): зеркало, NO SIGNAL, пробуждение
экрана «на охране» остаются как есть.

- **Эпизод заднего хода** начинается, когда `reversing` держится `REVERSE_CONFIRM_S` (1 с) подряд — короткий дребезг
  флага экран не дёргает; кончается, когда флаг сброшен. В начале эпизода экран с передней камерой / картой
  переключается на задний вид.
- **Обратно** (передняя камера / карта — как решит авто-логика navlogic): вперёд быстрее `FORWARD_MPS` (5 км/ч)
  `FORWARD_CONFIRM_S` подряд без `reversing`, или стоим `STOP_HOLD_S` (10 с): скорость ниже `STOP_MPS` или неизвестна
  (нет фикса), и `reversing` сброшен — медленный задний ход (Static Hold приёмника показывает 0) «стоянкой» не
  считается, пока хаб видит задний ход.
- **Ручной выбор главнее**: кнопка камеры (клавиша 4, кнопка GPS-модуля, админка) или клавиша карты во время эпизода —
  `manual()`: автоматика отпускает экран и ничего не трогает до начала следующего эпизода. Задний вид, включённый
  вручную до эпизода, автоматика не присваивает (и обратно не возвращает).
- Настройку или заднюю камеру выключили, пока задний вид держит автоматика, — обратно сразу.
"""

from __future__ import annotations

from typing import Literal

View = Literal["front", "rear"]

REVERSE_CONFIRM_S = 1.0  # задний ход держится столько — задний вид
FORWARD_MPS = 5.0 / 3.6  # вперёд быстрее этого …
FORWARD_CONFIRM_S = 0.5  # … столько подряд (одиночный скачок скорости GPS на стоянке не в счёт) — обратно
STOP_MPS = 0.5  # медленнее — «стоим»
STOP_HOLD_S = 10.0  # стоим столько — обратно


class AutoRear:
    def __init__(self) -> None:
        self.rev_since: float | None = None  # reversing непрерывно с этого момента
        self.episode = False  # эпизод заднего хода уже начался (до сброса reversing)
        self.active = False  # задний вид включила автоматика, и его ещё не отпустили
        self._fwd_since: float | None = None
        self._stop_since: float | None = None

    def manual(self) -> None:
        """Пользователь сам выбрал камеру: экран его до начала следующего эпизода заднего хода."""
        self._release()

    def _release(self) -> None:
        self.active = False
        self._fwd_since = self._stop_since = None

    def update(self, now: float, *, reversing: bool, speed_mps: float | None, enabled: bool,
               view: View) -> View | None:
        """Шаг автомата (now — монотонные часы). Возвращает вид, на который переключить экран, или None."""
        if reversing:
            if self.rev_since is None:
                self.rev_since = now
        else:
            self.rev_since = None
            self.episode = False

        if self.active and view != "rear":  # экран сменили мимо кнопки (выключили заднюю камеру) — не наш
            self._release()
        if self.active and not enabled:
            self._release()
            return "front"

        if self.rev_since is not None and not self.episode and now - self.rev_since >= REVERSE_CONFIRM_S:
            self.episode = True
            if enabled and view != "rear":
                self.active = True
                self._fwd_since = self._stop_since = None
                return "rear"
            return None  # задний вид уже на экране (вручную или с прошлого эпизода) или автоматика выключена

        if not self.active:
            return None
        forward = not reversing and speed_mps is not None and speed_mps > FORWARD_MPS
        stopped = not reversing and (speed_mps is None or speed_mps < STOP_MPS)
        self._fwd_since = (now if self._fwd_since is None else self._fwd_since) if forward else None
        self._stop_since = (now if self._stop_since is None else self._stop_since) if stopped else None
        if (self._fwd_since is not None and now - self._fwd_since >= FORWARD_CONFIRM_S) or (
                self._stop_since is not None and now - self._stop_since >= STOP_HOLD_S):
            self._release()
            return "front"
        return None
