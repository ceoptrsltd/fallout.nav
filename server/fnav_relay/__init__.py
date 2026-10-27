"""fnav-relay: серверная часть удалённого доступа fallout.nav (журнал позиций и вход, когда устройство не в сети;
push-уведомления охраны).

Стандартная библиотека Python, для push — ещё cryptography (apt python3-cryptography, импорт ленивый); пакет fnav
не импортируется. Контракты: docs/contracts/remote.md, раздел 2; docs/contracts/alarm.md, разделы 5–6.
"""

__version__ = "0.1.0"
