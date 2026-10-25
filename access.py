"""Доступ и оплата: пробный период, разовый перевод на карту, роли."""
import os

import storage as db


def _ids(name):
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}


def _flag(name):
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


# Пусто = бот открыт для всех. На время теста укажите свой Telegram ID.
ALLOWED = _ids("ALLOWED_USERS")
# Кому слать копии базы и кто может её восстанавливать (по умолчанию — ALLOWED_USERS).
BACKUP_TO = _ids("BACKUP_CHAT_IDS") or ALLOWED
# Кто подтверждает оплаты и выдаёт доступ (по умолчанию — те, кто получает копии базы).
ADMINS = _ids("ADMIN_IDS") or BACKUP_TO

PAID_MODE = _flag("PAID_MODE")
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3"))
PAY_CARD = os.getenv("PAY_CARD", "").strip()    # например: «2200 0000 0000 0000, Иван И.»
PAY_PRICE = os.getenv("PAY_PRICE", "").strip()  # например: «500 ₽»


def trial_left(uid):
    """Сколько секунд пробного периода осталось (может быть отрицательным)."""
    u = db.get_user(uid)
    now = db.ts(db.now())
    first = u["first_seen"] if u and u["first_seen"] else now
    return first + TRIAL_DAYS * 86400 - now


def has_access(uid):
    if not PAID_MODE or uid in ADMINS:
        return True
    u = db.get_user(uid)
    if u and u["paid"]:
        return True
    return trial_left(uid) > 0


def status_line(uid):
    """Текст про доступ для команды /access; None, если платный режим выключен."""
    if not PAID_MODE:
        return None
    if uid in ADMINS:
        return "Вы владелец: доступ без ограничений."
    u = db.get_user(uid)
    if u and u["paid"]:
        return "✅ Доступ открыт насовсем."
    left = trial_left(uid)
    if left > 0:
        days, hours = left // 86400, left % 86400 // 3600
        return f"Пробный период: осталось {days} дн. {hours} ч."
    return "Пробный период закончился."


def paywall_text():
    price = f" {PAY_PRICE}" if PAY_PRICE else ""
    card = PAY_CARD or "реквизиты пока не заданы, напишите владельцу"
    return (f"🔒 Пробный период ({TRIAL_DAYS} дн.) закончился.\n\n"
            f"Доступ насовсем открывается разовым переводом{price} на карту:\n{card}\n\n"
            "После перевода нажмите «Я оплатил(а)»: владелец проверит и откроет доступ. "
            "Ваши смены и отчёты сохранены.")
