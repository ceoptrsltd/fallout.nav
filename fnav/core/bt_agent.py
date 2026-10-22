"""Агент сопряжения BlueZ «NoInputNoOutput» (колонки без экрана и кнопок — Just Works).

Отдельный модуль без `from __future__ import annotations`: dbus-fast читает D-Bus-сигнатуры из аннотаций.
Соглашается только на сопряжение, которое начали мы сами (Pair() из админки) — чужие устройства рядом
сопрячься с машиной не могут.
"""
# ruff: noqa: F821

from dbus_fast import DBusError
from dbus_fast.service import ServiceInterface, method

REJECTED = "org.bluez.Error.Rejected"


class PairingAgent(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")
        self.allowed: set[str] = set()  # пути устройств, которые мы сейчас сопрягаем

    def _check(self, device: str) -> None:
        if device not in self.allowed:
            raise DBusError(REJECTED, "Pairing was not requested by fallout.nav")

    @method()
    def Release(self):
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        self._check(device)
        return "0000"

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        self._check(device)

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        self._check(device)
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        self._check(device)

    @method()
    def RequestAuthorization(self, device: "o"):
        self._check(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        self._check(device)  # доверенным (Trusted) BlueZ разрешает сам, без агента

    @method()
    def Cancel(self):
        pass
