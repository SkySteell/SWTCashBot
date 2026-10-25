import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

import access as A
import reports as R
import storage as db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shiftbot")

# Роли и платный режим настраиваются в access.py (переменные ALLOWED_USERS, ADMIN_IDS, PAID_MODE и др.)
ALLOWED = A.ALLOWED
REMIND_AT = tuple(int(x) for x in os.getenv("REMIND_AT", "23:30").split(":"))
BACKUP_TO = A.BACKUP_TO
BACKUP_AT = tuple(int(x) for x in os.getenv("BACKUP_AT", "23:50").split(":"))

router = Router()


async def allowed(event) -> bool:
    return not ALLOWED or event.from_user.id in ALLOWED


router.message.filter(allowed)
router.callback_query.filter(allowed)

PAY_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил(а)", callback_data="pay:sent")]])


class AccessMiddleware(BaseMiddleware):
    """В платном режиме пускает владельцев, оплативших и тех, у кого идёт пробный период."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        if A.ALLOWED and user.id not in A.ALLOWED:
            return None
        if not A.PAID_MODE:
            return await handler(event, data)
        db.touch_user(user.id)
        if A.has_access(user.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            if (event.data or "").startswith("pay:"):
                return await handler(event, data)
            await event.message.answer(A.paywall_text(), reply_markup=PAY_KB)
            await event.answer()
        else:
            await event.answer(A.paywall_text(), reply_markup=PAY_KB)
        return None


router.message.outer_middleware(AccessMiddleware())
router.callback_query.outer_middleware(AccessMiddleware())


def ib(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def ikb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


CANCEL = "❌ Отмена"
CANCEL_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=CANCEL)]], resize_keyboard=True)
KINDS = {"💰 Чаевые": "tip", "💵 Наличка": "cash", "🚗 Машина": "car", "⛽ Заправка": "fuel", "🧾 Прочее": "other"}
KIND_ICONS = {"tip": "💰", "cash": "💵", "car": "🚗", "fuel": "⛽", "other": "🧾"}


def main_kb(uid):
    if db.open_shift(uid):
        rows = [["💰 Чаевые", "💵 Наличка"], ["🚗 Машина", "⛽ Заправка", "🧾 Прочее"],
                ["📋 Записи смены", "🏁 Закрыть смену"], ["📊 Отчёты", "⚙️ Настройки"]]
    else:
        rows = [["▶️ Открыть смену"], ["📊 Отчёты", "⚙️ Настройки"]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


class S(StatesGroup):
    plan = State()
    open_odo = State()
    amount = State()
    note = State()
    litres = State()
    close_odo = State()
    close_yandex = State()
    close_orders = State()
    edit = State()


# ---------- разбор ввода ----------
def parse_num(t):
    t = (t or "").replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if 0 <= v < 1e9 else None


def parse_int(t):
    v = parse_num(t)
    return int(v) if v is not None and v == int(v) else None


def parse_dt(t, base):
    t = (t or "").strip()
    for f in ("%d.%m.%y %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(t, f).replace(tzinfo=db.TZ)
        except ValueError:
            pass
    try:
        hm = datetime.strptime(t, "%H:%M")
    except ValueError:
        return None
    return base.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)


# ---------- карточка смены ----------
def shift_context(s):
    ym = db.month_key(s["start_ts"])
    a, b = db.month_bounds(ym)
    allm = db.shifts_between(s["user_id"], a, b, closed=False)
    n = sum(1 for x in allm if x["start_ts"] <= s["start_ts"])
    closed = [x for x in allm if x["end_ts"] is not None and x["start_ts"] <= s["start_ts"]]
    ents = db.entries_for([x["id"] for x in closed])
    month_cash = sum(R.calc(x, ents.get(x["id"], []))["cash"] for x in closed)
    return n, db.get_plan(s["user_id"], ym), month_cash


def card(s):
    """Текст и клавиатура карточки смены (для просмотра и правки)."""
    entries = db.entries_for([s["id"]]).get(s["id"], [])
    is_open = s["end_ts"] is None
    if is_open:
        text = R.status_text(s, entries)
    else:
        n, plan, month_cash = shift_context(s)
        text = R.day_report(s, entries, n, plan, month_cash)
    text += "\n\n📋 Записи:\n" + R.entries_text(entries)
    rows = []
    for i, e in enumerate(entries, 1):
        rows.append([ib(f"✏️ {i}. {R.KIND_NAMES[e['kind']]} {R.fmt(e['amount'])}", f"ee:{e['id']}"),
                     ib("🗑", f"ed:{e['id']}")])
    rows.append([ib("➕ Запись", f"ea:{s['id']}")])
    if is_open:
        rows.append([ib("Нач. пробег", f"ef:{s['id']}:start_odo"), ib("Начало", f"ef:{s['id']}:start_ts")])
    else:
        rows.append([ib("Нач. пробег", f"ef:{s['id']}:start_odo"), ib("Кон. пробег", f"ef:{s['id']}:end_odo")])
        rows.append([ib("Яндекс", f"ef:{s['id']}:yandex"), ib("Заказы", f"ef:{s['id']}:orders")])
        rows.append([ib("Начало", f"ef:{s['id']}:start_ts"), ib("Конец", f"ef:{s['id']}:end_ts")])
    return text, ikb(rows)


async def show(c: CallbackQuery, text, kb=None):
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


def own_shift(c, sid):
    s = db.get_shift(int(sid))
    return s if s and s["user_id"] == c.from_user.id else None


# ---------- старт / отмена ----------
@router.message(F.text == CANCEL)
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=main_kb(m.from_user.id))


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    text = ("Привет! Я веду учёт смен: пробег, доходы, расходы и отчёты за день, месяц и год.\n"
            "Начните с «▶️ Открыть смену».")
    line = A.status_line(m.from_user.id)
    if line:
        text += f"\n\n{line}"
    await m.answer(text, reply_markup=main_kb(m.from_user.id))


# ---------- открытие смены ----------
@router.message(F.text == "▶️ Открыть смену")
async def open_start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    if db.open_shift(uid):
        await m.answer("Смена уже открыта.", reply_markup=main_kb(uid))
        return
    ym = db.month_key(db.ts(db.now()))
    if db.get_plan(uid, ym) is None:
        await state.set_state(S.plan)
        await state.update_data(ym=ym, then="open")
        await m.answer(f"Сколько плановых смен в этом месяце ({R.month_label(ym)})?", reply_markup=CANCEL_KB)
        return
    await state.set_state(S.open_odo)
    await m.answer("Начальный пробег (спидометр):", reply_markup=CANCEL_KB)


@router.message(S.plan)
async def got_plan(m: Message, state: FSMContext):
    v = parse_int(m.text)
    if v is None or not 1 <= v <= 31:
        await m.answer("Введите целое число от 1 до 31.")
        return
    d = await state.get_data()
    uid = m.from_user.id
    db.set_plan(uid, d["ym"], v)
    if d.get("then") == "open":
        await state.set_state(S.open_odo)
        await m.answer(f"План на {R.month_label(d['ym'])}: {v}. Теперь начальный пробег (спидометр):",
                       reply_markup=CANCEL_KB)
    else:
        await state.clear()
        await m.answer(f"✅ План на {R.month_label(d['ym'])}: {v} смен.", reply_markup=main_kb(uid))


@router.message(S.open_odo)
async def got_open_odo(m: Message, state: FSMContext):
    v = parse_num(m.text)
    if v is None:
        await m.answer("Введите пробег числом, например 123456.")
        return
    uid = m.from_user.id
    if db.open_shift(uid):
        await state.clear()
        await m.answer("Смена уже открыта.", reply_markup=main_kb(uid))
        return
    db.create_shift(uid, v)
    await state.clear()
    await m.answer(f"✅ Смена открыта в {db.now():%H:%M}. Пробег: {R.fmt(v)}.", reply_markup=main_kb(uid))


# ---------- записи: чаевые, наличка, расходы ----------
async def ask_amount(m: Message, state: FSMContext, shift_id, kind):
    await state.set_state(S.amount)
    await state.update_data(shift_id=shift_id, kind=kind)
    await m.answer(f"{R.KIND_NAMES[kind]}: введите сумму.", reply_markup=CANCEL_KB)


@router.message(F.text.in_(KINDS))
async def add_start(m: Message, state: FSMContext):
    await state.clear()
    s = db.open_shift(m.from_user.id)
    if not s:
        await m.answer("Сначала откройте смену.", reply_markup=main_kb(m.from_user.id))
        return
    await ask_amount(m, state, s["id"], KINDS[m.text])


@router.message(S.amount)
async def got_amount(m: Message, state: FSMContext):
    v = parse_num(m.text)
    if v is None or v <= 0:
        await m.answer("Введите сумму числом, например 350.")
        return
    d = await state.get_data()
    kind = d["kind"]
    await state.update_data(amount=v)
    if kind in ("car", "other"):
        await state.set_state(S.note)
        hint = "мойка, парковка, штраф…" if kind == "car" else "что именно"
        await m.answer(f"Пояснение ({hint}) или «-», чтобы пропустить:")
    elif kind == "fuel" and db.get_litres(m.from_user.id):
        await state.set_state(S.litres)
        await m.answer("Сколько литров? («-» — пропустить)")
    else:
        await save_entry(m, state)


@router.message(S.note)
async def got_note(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    await save_entry(m, state, note=None if t in ("", "-", "—") else t[:100])


@router.message(S.litres)
async def got_litres(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    litres = None
    if t not in ("-", "—", ""):
        litres = parse_num(t)
        if litres is None:
            await m.answer("Введите литры числом или «-».")
            return
    await save_entry(m, state, litres=litres)


async def save_entry(m: Message, state: FSMContext, note=None, litres=None):
    d = await state.get_data()
    uid = m.from_user.id
    s = db.get_shift(d["shift_id"])
    await state.clear()
    if not s or s["user_id"] != uid:
        await m.answer("Смена не найдена.", reply_markup=main_kb(uid))
        return
    db.add_entry(s["id"], uid, d["kind"], d["amount"], note, litres)
    text = f"✅ {R.KIND_NAMES[d['kind']]}: {R.fmt(d['amount'])}" + (f" ({note})" if note else "")
    await m.answer(text, reply_markup=main_kb(uid))
    if s["end_ts"] is not None:
        t, kb = card(s)
        await m.answer(t, reply_markup=kb)


@router.message(F.text == "📋 Записи смены")
async def entries_view(m: Message, state: FSMContext):
    await state.clear()
    s = db.open_shift(m.from_user.id)
    if not s:
        await m.answer("Открытой смены нет.", reply_markup=main_kb(m.from_user.id))
        return
    t, kb = card(s)
    await m.answer(t, reply_markup=kb)


# ---------- закрытие смены ----------
@router.message(F.text == "🏁 Закрыть смену")
async def close_start(m: Message, state: FSMContext):
    await state.clear()
    s = db.open_shift(m.from_user.id)
    if not s:
        await m.answer("Открытой смены нет.", reply_markup=main_kb(m.from_user.id))
        return
    await state.set_state(S.close_odo)
    await state.update_data(shift_id=s["id"])
    await m.answer(f"Конечный пробег (спидометр)? Начальный был {R.fmt(s['start_odo'])}.", reply_markup=CANCEL_KB)


@router.message(S.close_odo)
async def got_close_odo(m: Message, state: FSMContext):
    v = parse_num(m.text)
    d = await state.get_data()
    s = db.get_shift(d["shift_id"])
    if v is None:
        await m.answer("Введите пробег числом.")
        return
    if v < s["start_odo"]:
        await m.answer("Конечный пробег не может быть меньше начального. Введите ещё раз.")
        return
    await state.update_data(end_odo=v)
    await state.set_state(S.close_yandex)
    await m.answer("Сумма из Яндекса за смену (без комиссии и налога):")


@router.message(S.close_yandex)
async def got_close_yandex(m: Message, state: FSMContext):
    v = parse_num(m.text)
    if v is None:
        await m.answer("Введите сумму числом.")
        return
    await state.update_data(yandex=v)
    await state.set_state(S.close_orders)
    await m.answer("Сколько было заказов?")


@router.message(S.close_orders)
async def got_close_orders(m: Message, state: FSMContext):
    v = parse_int(m.text)
    if v is None:
        await m.answer("Введите число заказов, например 5.")
        return
    d = await state.get_data()
    uid = m.from_user.id
    db.close_shift(d["shift_id"], d["end_odo"], d["yandex"], v)
    await state.clear()
    s = db.get_shift(d["shift_id"])
    entries = db.entries_for([s["id"]]).get(s["id"], [])
    n, plan, month_cash = shift_context(s)
    await m.answer("🏁 Смена закрыта.", reply_markup=main_kb(uid))
    await m.answer(R.day_report(s, entries, n, plan, month_cash),
                   reply_markup=ikb([[ib("✏️ Править смену", f"sh:{s['id']}")]]))


# ---------- правка смен и записей ----------
@router.callback_query(F.data.startswith("sh:"))
async def open_card(c: CallbackQuery):
    s = own_shift(c, c.data.split(":")[1])
    if s:
        t, kb = card(s)
        await c.message.answer(t, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("ed:"))
async def entry_delete(c: CallbackQuery):
    e = db.get_entry(int(c.data.split(":")[1]))
    if e and e["user_id"] == c.from_user.id:
        db.delete_entry(e["id"])
        t, kb = card(db.get_shift(e["shift_id"]))
        await show(c, t, kb)
    await c.answer("Удалено")


@router.callback_query(F.data.startswith("ee:"))
async def entry_edit(c: CallbackQuery, state: FSMContext):
    e = db.get_entry(int(c.data.split(":")[1]))
    if e and e["user_id"] == c.from_user.id:
        await state.set_state(S.edit)
        await state.update_data(what="entry", id=e["id"])
        await c.message.answer(f"{R.KIND_NAMES[e['kind']]}: сейчас {R.fmt(e['amount'])}. Введите новую сумму:",
                               reply_markup=CANCEL_KB)
    await c.answer()


FIELD_PROMPT = {
    "start_odo": "Новый начальный пробег:", "end_odo": "Новый конечный пробег:",
    "yandex": "Новая сумма из Яндекса:", "orders": "Новое число заказов:",
    "start_ts": "Новое время начала (чч:мм или дд.мм.гг чч:мм):",
    "end_ts": "Новое время окончания (чч:мм или дд.мм.гг чч:мм):",
}


@router.callback_query(F.data.startswith("ef:"))
async def field_edit(c: CallbackQuery, state: FSMContext):
    _, sid, field = c.data.split(":")
    s = own_shift(c, sid)
    if s and field in FIELD_PROMPT:
        await state.set_state(S.edit)
        await state.update_data(what="shift", id=s["id"], field=field)
        await c.message.answer(FIELD_PROMPT[field], reply_markup=CANCEL_KB)
    await c.answer()


@router.callback_query(F.data.startswith("ea:"))
async def entry_add_pick(c: CallbackQuery):
    s = own_shift(c, c.data.split(":")[1])
    if s:
        rows = [[ib(f"{KIND_ICONS[k]} {R.KIND_NAMES[k]}", f"ek:{s['id']}:{k}") for k in ("tip", "cash")],
                [ib(f"{KIND_ICONS[k]} {R.KIND_NAMES[k]}", f"ek:{s['id']}:{k}") for k in ("car", "fuel", "other")]]
        await c.message.answer("Какую запись добавить?", reply_markup=ikb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("ek:"))
async def entry_add_kind(c: CallbackQuery, state: FSMContext):
    _, sid, kind = c.data.split(":")
    s = own_shift(c, sid)
    if s and kind in R.KIND_NAMES:
        await ask_amount(c.message, state, s["id"], kind)
    await c.answer()


@router.message(S.edit)
async def got_edit(m: Message, state: FSMContext):
    d = await state.get_data()
    uid = m.from_user.id
    if d["what"] == "entry":
        v = parse_num(m.text)
        if v is None or v <= 0:
            await m.answer("Введите сумму числом.")
            return
        e = db.get_entry(d["id"])
        if not e or e["user_id"] != uid:
            await state.clear()
            return
        db.update_entry(e["id"], v)
        sid = e["shift_id"]
    else:
        s = db.get_shift(d["id"])
        if not s or s["user_id"] != uid:
            await state.clear()
            return
        field, sid = d["field"], s["id"]
        if field in ("start_ts", "end_ts"):
            base = db.dt(s[field]) if s[field] else db.now()
            new = parse_dt(m.text, base)
            if new is None:
                await m.answer("Не понял время. Пример: 09:15 или 09.09.26 09:15")
                return
            start = new if field == "start_ts" else db.dt(s["start_ts"])
            if field == "end_ts" and new <= start:
                new += timedelta(days=1)
            if field == "start_ts" and s["end_ts"] and new >= db.dt(s["end_ts"]):
                await m.answer("Начало должно быть раньше окончания.")
                return
            value = db.ts(new)
        elif field == "orders":
            value = parse_int(m.text)
        else:
            value = parse_num(m.text)
        if value is None:
            await m.answer("Введите число.")
            return
        db.update_shift(sid, field, value)
    await state.clear()
    await m.answer("✅ Сохранено.", reply_markup=main_kb(uid))
    t, kb = card(db.get_shift(sid))
    await m.answer(t, reply_markup=kb)


# ---------- отчёты ----------
REPORTS_KB = ikb([[ib("📅 Текущий месяц", "r:m:cur"), ib("🗓 Текущий год", "r:y:cur")],
                  [ib("📆 Другие месяцы", "r:ml"), ib("📚 Другие годы", "r:yl")],
                  [ib("🗂 Смены текущего месяца", "r:sl:cur")]])


def cur_ym():
    return db.now().strftime("%Y-%m")


def month_view(uid, ym):
    a, b = db.month_bounds(ym)
    shifts = db.shifts_between(uid, a, b)
    ents = db.entries_for([s["id"] for s in shifts])
    calcs = [R.calc(s, ents.get(s["id"], [])) for s in shifts]
    text = R.period_report("month", R.month_label(ym), calcs)
    kb = ikb([[ib("📥 Excel", f"x:m:{ym}"), ib("🗂 Смены", f"r:sl:{ym}")]]) if shifts else None
    return text, kb


def year_view(uid, y):
    a, b = db.year_bounds(y)
    shifts = db.shifts_between(uid, a, b)
    ents = db.entries_for([s["id"] for s in shifts])
    calcs = [R.calc(s, ents.get(s["id"], [])) for s in shifts]
    by_month = {}
    for s, c in zip(shifts, calcs):
        by_month.setdefault(db.month_key(s["start_ts"]), []).append(c)
    text = R.period_report("year", str(y), calcs, by_month)
    kb = ikb([[ib("📥 Excel", f"x:y:{y}")]]) if shifts else None
    return text, kb


@router.message(F.text == "📊 Отчёты")
async def reports_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Какой отчёт показать?", reply_markup=REPORTS_KB)


@router.message(Command("month"))
async def cmd_month(m: Message):
    t, kb = month_view(m.from_user.id, cur_ym())
    await m.answer(t, reply_markup=kb)


@router.message(Command("year"))
async def cmd_year(m: Message):
    t, kb = year_view(m.from_user.id, db.now().year)
    await m.answer(t, reply_markup=kb)


@router.callback_query(F.data.startswith("r:"))
async def reports_cb(c: CallbackQuery):
    uid = c.from_user.id
    parts = c.data.split(":")
    act = parts[1]
    key = parts[2] if len(parts) > 2 else None
    if act == "m":
        t, kb = month_view(uid, cur_ym() if key == "cur" else key)
        await show(c, t, kb)
    elif act == "y":
        t, kb = year_view(uid, db.now().year if key == "cur" else int(key))
        await show(c, t, kb)
    elif act == "ml":
        months, _ = db.periods(uid)
        if not months:
            await show(c, "Закрытых смен пока нет.", REPORTS_KB)
        else:
            btns = [ib(f"{R.MON_SHORT[int(ym[5:]) - 1]} {ym[:4]}", f"r:m:{ym}") for ym in months[:24]]
            await show(c, "Выберите месяц:", ikb([btns[i:i + 3] for i in range(0, len(btns), 3)]))
    elif act == "yl":
        _, years = db.periods(uid)
        if not years:
            await show(c, "Закрытых смен пока нет.", REPORTS_KB)
        else:
            btns = [ib(str(y), f"r:y:{y}") for y in years]
            await show(c, "Выберите год:", ikb([btns[i:i + 4] for i in range(0, len(btns), 4)]))
    elif act == "sl":
        ym = cur_ym() if key == "cur" else key
        a, b = db.month_bounds(ym)
        shifts = db.shifts_between(uid, a, b, closed=False)
        if not shifts:
            await show(c, f"В {R.month_label(ym)} смен нет.", REPORTS_KB)
        else:
            ents = db.entries_for([s["id"] for s in shifts])
            rows = []
            for i, s in enumerate(shifts, 1):
                d = db.dt(s["start_ts"])
                label = f"{i}. {R.WD[d.weekday()]} {d:%d.%m}"
                label += (f" · {R.fmt(R.calc(s, ents.get(s['id'], []))['cash'])}" if s["end_ts"] else " · открыта")
                rows.append([ib(label, f"sh:{s['id']}")])
            await show(c, f"Смены: {R.month_label(ym)}. Нажмите, чтобы посмотреть или исправить.", ikb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("x:"))
async def excel_cb(c: CallbackQuery):
    uid = c.from_user.id
    _, kind, key = c.data.split(":")
    if kind == "m":
        a, b = db.month_bounds(key)
        title, fname = R.month_label(key), f"smeny_{key}.xlsx"
    else:
        a, b = db.year_bounds(int(key))
        title, fname = key, f"smeny_{key}.xlsx"
    shifts = db.shifts_between(uid, a, b)
    ents = db.entries_for([s["id"] for s in shifts])
    items = [(s, R.calc(s, ents.get(s["id"], []))) for s in shifts]
    by_month = None
    if kind == "y":
        by_month = {}
        for s, cc in items:
            by_month.setdefault(db.month_key(s["start_ts"]), []).append(cc)
    data = R.build_xlsx(title, items, ents, by_month)
    await c.message.answer_document(BufferedInputFile(data, filename=fname), caption=f"Отчёт: {title}")
    await c.answer()


# ---------- настройки ----------
def settings_kb(uid):
    on = db.get_litres(uid)
    rows = [[ib(f"⛽ Литры при заправке: {'вкл' if on else 'выкл'}", "s:lit")],
            [ib("📋 Плановые смены этого месяца", "s:plan")]]
    if uid in BACKUP_TO:
        rows.append([ib(f"💾 Ежедневная копия базы: {'вкл' if db.get_backup(uid) else 'выкл'}", "s:bak")])
    return ikb(rows)


@router.message(F.text == "⚙️ Настройки")
async def settings(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Настройки:", reply_markup=settings_kb(m.from_user.id))


@router.callback_query(F.data.startswith("s:"))
async def settings_cb(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    act = c.data.split(":")[1]
    if act == "lit":
        db.set_litres(uid, not db.get_litres(uid))
        await show(c, "Настройки:", settings_kb(uid))
    elif act == "bak" and uid in BACKUP_TO:
        db.set_backup(uid, not db.get_backup(uid))
        await show(c, "Настройки:", settings_kb(uid))
    elif act == "plan":
        ym = cur_ym()
        cur = db.get_plan(uid, ym)
        await state.set_state(S.plan)
        await state.update_data(ym=ym, then="settings")
        note = f" Сейчас: {cur}." if cur else ""
        await c.message.answer(f"Плановых смен в {R.month_label(ym)}?{note}", reply_markup=CANCEL_KB)
    await c.answer()


# ---------- оплата и доступ ----------
@router.message(Command("access"))
async def cmd_access(m: Message):
    line = A.status_line(m.from_user.id)
    await m.answer(line or "Платный режим выключен: бот доступен всем.")


PAY_REQUESTED = {}


@router.callback_query(F.data == "pay:sent")
async def pay_sent(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    now = db.ts(db.now())
    if now - PAY_REQUESTED.get(uid, 0) < 300:
        await c.answer("Заявка уже отправлена, подождите.", show_alert=True)
        return
    if not A.ADMINS:
        await c.answer("Владелец не настроен, напишите ему напрямую.", show_alert=True)
        return
    PAY_REQUESTED[uid] = now
    who = c.from_user.full_name + (f" (@{c.from_user.username})" if c.from_user.username else "")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Открыть доступ", callback_data=f"pay:ok:{uid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pay:no:{uid}")]])
    for admin in A.ADMINS:
        try:
            await bot.send_message(admin, f"💳 {who}, id {uid}, сообщает об оплате. Проверьте перевод.",
                                   reply_markup=kb)
        except Exception:
            log.exception("не удалось отправить заявку владельцу")
    await c.message.answer("Заявка отправлена. Как только владелец проверит перевод, я напишу.")
    await c.answer()


@router.callback_query(F.data.startswith("pay:ok:") | F.data.startswith("pay:no:"))
async def pay_decide(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in A.ADMINS:
        await c.answer("Нет прав.", show_alert=True)
        return
    _, verdict, raw = c.data.split(":")
    uid = int(raw)
    if verdict == "ok":
        db.set_paid(uid, True)
        result, note = "✅ Доступ открыт", "✅ Оплата подтверждена, доступ открыт насовсем. Спасибо!"
    else:
        result, note = "❌ Отклонено", ("Не нашёл ваш перевод. Если вы оплатили, напишите владельцу "
                                       "и нажмите «Я оплатил(а)» ещё раз.")
    try:
        await bot.send_message(uid, note, reply_markup=main_kb(uid))
    except Exception:
        log.exception("не удалось написать пользователю")
    await show(c, f"{c.message.text}\n\n{result}")
    await c.answer()


def _uid_arg(m: Message):
    parts = (m.text or "").split()
    return int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None


@router.message(Command("grant"))
async def cmd_grant(m: Message):
    if m.from_user.id not in A.ADMINS:
        return
    uid = _uid_arg(m)
    if uid is None:
        await m.answer("Использование: /grant 123456789")
        return
    db.set_paid(uid, True)
    await m.answer(f"✅ Доступ насовсем открыт для {uid}.")


@router.message(Command("revoke"))
async def cmd_revoke(m: Message):
    if m.from_user.id not in A.ADMINS:
        return
    uid = _uid_arg(m)
    if uid is None:
        await m.answer("Использование: /revoke 123456789")
        return
    db.set_paid(uid, False)
    await m.answer(f"Платный доступ для {uid} снят (если пробный период кончился, бот попросит оплату).")


@router.message(Command("users"))
async def cmd_users(m: Message):
    if m.from_user.id not in A.ADMINS:
        return
    rows = db.all_users()
    paid = sum(1 for r in rows if r["paid"])
    trial = sum(1 for r in rows if not r["paid"] and A.trial_left(r["user_id"]) > 0)
    await m.answer(f"Пользователей: {len(rows)}\nОплатили: {paid}\nНа пробном периоде: {trial}\n"
                   f"Пробный период закончился: {len(rows) - paid - trial}\n"
                   f"Платный режим: {'вкл' if A.PAID_MODE else 'выкл'}")


# ---------- копия базы ----------
async def send_backup(bot: Bot, chat_id, caption="Копия базы. Чтобы восстановить, отправьте этот файл боту."):
    data = db.backup_bytes()
    name = f"shiftbot_{db.now():%Y%m%d_%H%M}.db"
    await bot.send_document(chat_id, BufferedInputFile(data, filename=name), caption=caption)


@router.message(Command("backup"))
async def cmd_backup(m: Message, bot: Bot):
    if m.from_user.id not in BACKUP_TO:
        await m.answer("Копии баз включаются переменной BACKUP_CHAT_IDS (ваш Telegram ID).")
        return
    await send_backup(bot, m.from_user.id)


@router.message(F.document)
async def restore_backup(m: Message, bot: Bot, state: FSMContext):
    if m.from_user.id not in BACKUP_TO or not (m.document.file_name or "").lower().endswith(".db"):
        return
    await state.clear()
    tmp = os.path.join(os.path.dirname(db.DB_PATH) or ".", "restore_tmp.db")
    await bot.download(m.document, destination=tmp)
    try:
        await send_backup(bot, m.from_user.id, caption="Текущая база перед восстановлением (на случай ошибки).")
        ok = db.restore_from(tmp)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    await m.answer("✅ База восстановлена." if ok else "Это не похоже на базу бота, ничего не менял.",
                   reply_markup=main_kb(m.from_user.id))


# ---------- напоминание о незакрытой смене ----------
async def reminder_loop(bot: Bot):
    while True:
        try:
            t = db.now()
            if (t.hour, t.minute) >= REMIND_AT:
                day = t.strftime("%Y-%m-%d")
                for s in db.open_shifts_unreminded(day):
                    started = db.dt(s["start_ts"])
                    try:
                        await bot.send_message(
                            s["user_id"],
                            f"⏰ Смена от {started:%d.%m %H:%M} ещё не закрыта. Если закончили, нажмите "
                            "«🏁 Закрыть смену». Если ещё в поездке, ничего не делайте: сам я её не закрою.")
                    except Exception:
                        log.exception("не удалось отправить напоминание")
                    db.mark_reminded(s["id"], day)
        except Exception:
            log.exception("ошибка в цикле напоминаний")
        await asyncio.sleep(60)


async def backup_loop(bot: Bot):
    last_day = None
    while True:
        try:
            t = db.now()
            day = t.strftime("%Y-%m-%d")
            if (t.hour, t.minute) >= BACKUP_AT and last_day != day:
                last_day = day
                for chat_id in [u for u in db.backup_users() if u in BACKUP_TO]:
                    try:
                        await send_backup(bot, chat_id)
                    except Exception:
                        log.exception("не удалось отправить копию базы")
        except Exception:
            log.exception("ошибка в цикле копий")
        await asyncio.sleep(60)


async def main():
    db.init()
    if A.PAID_MODE and not A.ADMINS:
        log.error("PAID_MODE включён, но ADMIN_IDS не задан: заявки на оплату некому подтверждать")
    bot = Bot(token=os.environ["BOT_TOKEN"])
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    tasks = [asyncio.create_task(reminder_loop(bot)), asyncio.create_task(backup_loop(bot))]
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
