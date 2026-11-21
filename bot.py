import asyncio
import base64
import calendar
import html
import json
import logging
import math
import random
import sqlite3
import time as _time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

from config import *  # noqa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zayberman")

ATZ = ZoneInfo(TZ_ADMIN)
CTZ = ZoneInfo(TZ_CLIENT)
UTC = timezone.utc
FMT = "%Y-%m-%d %H:%M"
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
          "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
ACTIVE = ("hold", "review", "paid")
STATUS = {"hold": "⏳ ждёт оплаты", "review": "🔎 оплата на проверке", "paid": "✅ оплачено"}

PAY_METHODS = {"pp": "PayPal", "da": "DonationAlerts", "tr": "Tribute", "rv": "Revolut / банк", "cr": "USDT / GRAM"}

BTN_BOOK, BTN_MY, BTN_CONTACT, BTN_ADMIN = "📝 Запись", "📋 Брони", "💬 Связаться", "🔒 Админка"

router = Router()


class Shot(StatesGroup):
    wait = State()


class AdminSt(StatesGroup):
    bcast = State()
    ban = State()


# ───────────────────────── DB ─────────────────────────
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS bookings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE, user_id INTEGER, username TEXT, full_name TEXT,
            start_utc TEXT, status TEXT, created_ts INTEGER, hold_until_ts INTEGER,
            price_rub INTEGER, price_usd INTEGER, price_eur INTEGER,
            pay_note TEXT, rem24 INTEGER DEFAULT 0, rem1 INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS ix_b_start ON bookings(start_utc);
        CREATE TABLE IF NOT EXISTS closed_days(day TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS closed_hours(day TEXT, hour INTEGER, PRIMARY KEY(day, hour));
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, first_ts INTEGER);
        CREATE TABLE IF NOT EXISTS chain_seen(h TEXT PRIMARY KEY);
        """)
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        if "banned" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")


def q(sql, args=(), one=False):
    with db() as c:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    with db() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def get_b(bid):
    return q("SELECT * FROM bookings WHERE id=?", (bid,), one=True)


# ───────────────────────── time ─────────────────────────
def now_utc():
    return datetime.now(UTC).replace(tzinfo=None)


def today_admin():
    return datetime.now(ATZ).date()


def u2(s):  # str -> aware utc
    return datetime.strptime(s, FMT).replace(tzinfo=UTC)


def local_to_utc(d, minutes):
    loc = datetime.combine(d, dtime(0), tzinfo=ATZ) + timedelta(minutes=minutes)
    return loc.astimezone(UTC).replace(tzinfo=None)


def fmt_client(s):
    t = u2(s).astimezone(CTZ)
    return f"{t:%d.%m} ({WD[t.weekday()]}) {t:%H:%M} {TZ_CLIENT_LABEL}"


def fmt_admin(s):
    a = u2(s).astimezone(ATZ)
    c = u2(s).astimezone(CTZ)
    res = f"{a:%d.%m} ({WD[a.weekday()]}) {a:%H:%M} {TZ_ADMIN_LABEL}"
    if c.strftime("%H:%M") != a.strftime("%H:%M"):
        res += f" ({c:%H:%M} {TZ_CLIENT_LABEL})"
    return res


def esc(s):
    return html.escape(s or "")


def who(b):
    if b["username"]:
        return f"@{esc(b['username'])}"
    return f'<a href="tg://user?id={b["user_id"]}">{esc(b["full_name"]) or b["user_id"]}</a>'


# ───────────────────────── schedule ─────────────────────────
def day_closed(ds):
    return q("SELECT 1 FROM closed_days WHERE day=?", (ds,), one=True) is not None


def closed_hours(ds):
    return {r["hour"] for r in q("SELECT hour FROM closed_hours WHERE day=?", (ds,))}


def active_between(a_utc, b_utc):
    return q(f"SELECT * FROM bookings WHERE status IN {ACTIVE} AND start_utc>=? AND start_utc<? ORDER BY start_utc",
             (a_utc.strftime(FMT), b_utc.strftime(FMT)))


def free_slots(d, exclude_id=None):
    ds = d.isoformat()
    if day_closed(ds):
        return []
    ch = closed_hours(ds)
    lo = local_to_utc(d, 0) - timedelta(hours=4)
    hi = local_to_utc(d, 24 * 60) + timedelta(hours=4)
    busy = [datetime.strptime(b["start_utc"], FMT) for b in active_between(lo, hi) if b["id"] != exclude_id]
    min_start = now_utc() + timedelta(minutes=MIN_LEAD_MIN)
    gap = (DURATION + BREAK) * 60
    out = []
    for m in range(DAY_START, LAST_START + 1, SLOT_STEP):
        if {m // 60, (m + DURATION - 1) // 60} & ch:
            continue
        s = local_to_utc(d, m)
        if s < min_start:
            continue
        if any(abs((s - b).total_seconds()) < gap for b in busy):
            continue
        out.append(s)
    return out


def bookable_day(d):
    t = today_admin()
    return t <= d <= t + timedelta(days=HORIZON_DAYS)


# ───────────────────────── prices ─────────────────────────
_rates = {"ts": 0, "usd": None, "eur": None, "ton": None, "ton_ts": 0}


async def http_json(url, params=None, headers=None):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url, params=params, headers=headers) as r:
            return await r.json(content_type=None)


async def prices():
    if _time.time() - _rates["ts"] > 6 * 3600 or not _rates["usd"]:
        try:
            j = await http_json("https://www.cbr-xml-daily.ru/daily_json.js")
            v = j["Valute"]
            _rates["usd"] = v["USD"]["Value"] / v["USD"]["Nominal"]
            _rates["eur"] = v["EUR"]["Value"] / v["EUR"]["Nominal"]
            _rates["ts"] = _time.time()
        except Exception:
            log.exception("rates")
    usd = max(1, round(PRICE_RUB / _rates["usd"])) if _rates["usd"] else 25
    eur = max(1, round(PRICE_RUB / _rates["eur"])) if _rates["eur"] else 22
    return usd, eur


async def ton_price():
    if _time.time() - _rates["ton_ts"] > 600 or not _rates["ton"]:
        try:
            j = await http_json("https://api.coingecko.com/api/v3/simple/price",
                                {"ids": "the-open-network", "vs_currencies": "usd"})
            _rates["ton"] = float(j["the-open-network"]["usd"])
            _rates["ton_ts"] = _time.time()
        except Exception:
            log.exception("ton price")
    return _rates["ton"]


async def price_line():
    usd, eur = await prices()
    return f"{PRICE_RUB} ₽ (≈ {usd} $ / {eur} €)"


# ───────────────────────── keyboards ─────────────────────────
def main_kb(uid):
    rows = [[KeyboardButton(text=BTN_BOOK), KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_CONTACT)]]
    if uid == ADMIN_ID:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def ib(text, data=None, url=None):
    return InlineKeyboardButton(text=text, url=url) if url else InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def month_grid(y, m, label_fn, nav_prefix, min_ym, max_ym):
    rows = [[ib(f"{MONTHS[m]} {y}", "x")], [ib(w, "x") for w in WD]]
    for week in calendar.Calendar().monthdayscalendar(y, m):
        row = []
        for d in week:
            row.append(ib(" ", "x") if d == 0 else label_fn(date(y, m, d)))
        rows.append(row)
    prev_y, prev_m = (y - 1, 12) if m == 1 else (y, m - 1)
    next_y, next_m = (y + 1, 1) if m == 12 else (y, m + 1)
    nav = []
    nav.append(ib("◀️", f"{nav_prefix}:{prev_y}-{prev_m:02d}") if (prev_y, prev_m) >= min_ym else ib(" ", "x"))
    nav.append(ib("▶️", f"{nav_prefix}:{next_y}-{next_m:02d}") if (next_y, next_m) <= max_ym else ib(" ", "x"))
    rows.append(nav)
    return rows


def ym_bounds():
    t = today_admin()
    e = t + timedelta(days=HORIZON_DAYS)
    return (t.year, t.month), (e.year, e.month)


def client_cal(y, m, rid):
    mn, mx = ym_bounds()

    def lab(d):
        if bookable_day(d) and free_slots(d, rid or None):
            return ib(str(d.day), f"cd:{d.isoformat()}:{rid}")
        return ib("·", "x")
    rows = month_grid(y, m, lab, f"cc:{rid}", mn, mx)
    if rid:
        rows.append([ib("⬅️ Назад", f"mb:{rid}")])
    return kb(rows)


def parse_ym(s):
    y, m = s.split("-")
    return int(y), int(m)


# ───────────────────────── payment ─────────────────────────
async def pay_text(b):
    hold_left = max(0, int((b["hold_until_ts"] - _time.time()) // 60))
    tp = await ton_price()
    gram_line = f" или <code>{math.ceil(b['price_usd'] / tp * 100) / 100:g}</code> GRAM" if tp else ""
    return (
        f"🧾 <b>Бронь {b['code']}</b>\n"
        f"📅 {fmt_client(b['start_utc'])}\n"
        f"💰 {b['price_rub']} ₽ ≈ <b>{b['price_usd']} $</b> ≈ <b>{b['price_eur']} €</b>\n\n"
        f"<b>Способы оплаты:</b>\n"
        f"• PayPal: {PAYPAL}\n"
        f"• DonationAlerts: {DALINK}\n"
        f"• Tribute (оплата в Telegram): {TRIBUTE}\n"
        f"• Revolut / банк: {REVOLUT_NAME}\n  IBAN <code>{REVOLUT_IBAN}</code>\n"
        f"• <b>USDT или GRAM</b> (сеть TON) — из любого кошелька: Gram Wallet в Telegram, Tonkeeper, Walt и др.\n"
        f"  Сумма: <code>{b['price_usd']}</code> USDT{gram_line}\n"
        f"  Адрес: <code>{TON_ADDR}</code>\n"
        f"  Комментарий: <code>{b['code']}</code>\n"
        f"  ⚠️ С этим комментарием оплата подтвердится автоматически.\n"
        f"  (нажмите на сумму, адрес или код, чтобы скопировать)\n\n"
        f"Другие способы: по возможности укажите код <code>{b['code']}</code> в комментарии "
        f"и после оплаты нажмите «✅ Я оплатил».\n\n"
        f"⏳ Бронь держится ещё ~{hold_left // 60} ч {hold_left % 60} мин, затем время освободится."
    )


async def pay_kb(b):
    return kb([
        [ib("✅ Я оплатил", f"paid:{b['id']}")],
        [ib("❌ Отменить бронь", f"mbx:{b['id']}")],
    ])


# ───────────────────────── notify ─────────────────────────
async def to_admin(bot, text, markup=None):
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        log.exception("to_admin")


async def to_user(bot, uid, text, markup=None):
    try:
        await bot.send_message(uid, text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:
        log.exception("to_user")


async def mark_paid(bot, bid, via):
    b = get_b(bid)
    if not b or b["status"] not in ("hold", "review"):
        return False
    ex("UPDATE bookings SET status='paid', pay_note=COALESCE(pay_note,'')||? WHERE id=?", (f" {via}", bid))
    await to_user(bot, b["user_id"],
                  f"✅ <b>Оплата подтверждена!</b>\n\nКонсультация: {fmt_client(b['start_utc'])}\n"
                  f"В назначенное время я позвоню вам в Telegram. Напомню за 24 часа и за 1 час.")
    return True


# ───────────────────────── client handlers ─────────────────────────
def save_user(u):
    ex("INSERT OR IGNORE INTO users(user_id, username, full_name, first_ts) VALUES(?,?,?,?)",
       (u.id, u.username, u.full_name, int(_time.time())))
    ex("UPDATE users SET username=?, full_name=? WHERE user_id=?", (u.username, u.full_name, u.id))


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    save_user(m.from_user)
    await m.answer(WELCOME.format(price=await price_line()), reply_markup=main_kb(m.from_user.id))


@router.message(F.text == BTN_CONTACT)
async def contact(m: Message):
    await m.answer(f"💬 Написать мне напрямую: @{ADMIN_USERNAME}",
                   reply_markup=kb([[ib("✉️ Написать", url=f"https://t.me/{ADMIN_USERNAME}")]]))


@router.message(F.text == BTN_BOOK)
async def book(m: Message, state: FSMContext):
    await state.clear()
    save_user(m.from_user)
    hold = q("SELECT * FROM bookings WHERE user_id=? AND status='hold'", (m.from_user.id,), one=True)
    if hold:
        await m.answer("У вас уже есть неоплаченная бронь. Оплатите или отмените её, чтобы записаться ещё раз.")
        await m.answer(await pay_text(hold), reply_markup=await pay_kb(hold), disable_web_page_preview=True)
        return
    t = today_admin()
    await m.answer(f"📅 Выберите день (консультация 60 мин, {await price_line()}):",
                   reply_markup=client_cal(t.year, t.month, 0))


def my_list(uid):
    lo = (now_utc() - timedelta(minutes=DURATION)).strftime(FMT)
    return q(f"SELECT * FROM bookings WHERE user_id=? AND status IN {ACTIVE} AND start_utc>=? ORDER BY start_utc",
             (uid, lo))


def my_view(uid):
    bs = my_list(uid)
    if not bs:
        return "У вас нет активных броней.", kb([[ib("📝 Записаться", "newbook")]])
    rows = [[ib(f"{fmt_client(b['start_utc'])} · {STATUS[b['status']]}", f"mb:{b['id']}")] for b in bs]
    return "📋 <b>Ваши брони</b> — нажмите, чтобы перенести или отменить:", kb(rows)


@router.message(F.text == BTN_MY)
async def my(m: Message, state: FSMContext):
    await state.clear()
    t, k = my_view(m.from_user.id)
    await m.answer(t, reply_markup=k)


async def handle_shot(m: Message, bid):
    b = get_b(bid)
    if not b or b["user_id"] != m.from_user.id or b["status"] not in ("hold", "review"):
        await m.answer("Эта бронь уже неактивна.")
        return
    ex("UPDATE bookings SET status='review' WHERE id=?", (bid,))
    cap = (f"💳 {who(b)} прислал скрин оплаты\nБронь {b['code']}: {fmt_admin(b['start_utc'])}\n"
           f"Сумма: {b['price_rub']} ₽ / {b['price_usd']} $ / {b['price_eur']} €")
    mk = kb([[ib("✅ Подтвердить", f"aok:{bid}"), ib("❌ Отклонить", f"ano:{bid}")]])
    try:
        if m.photo:
            await m.bot.send_photo(ADMIN_ID, m.photo[-1].file_id, caption=cap, reply_markup=mk)
        elif m.document:
            await m.bot.send_document(ADMIN_ID, m.document.file_id, caption=cap, reply_markup=mk)
    except Exception:
        log.exception("shot to admin")
    await m.answer("👍 Скрин получен, спасибо! Подтвержу бронь после проверки.",
                   reply_markup=main_kb(m.from_user.id))


@router.message(AdminSt.bcast)
async def admin_bcast_in(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    if m.text in (BTN_BOOK, BTN_MY, BTN_CONTACT, BTN_ADMIN):
        await state.clear()
        return await route_text(m, state)
    await state.clear()
    n = q("SELECT COUNT(*) n FROM users WHERE COALESCE(banned,0)=0 AND user_id!=?", (ADMIN_ID,), one=True)["n"]
    await m.answer("👆 Так увидят сообщение пользователи. Отправить?")
    await m.bot.copy_message(m.chat.id, m.chat.id, m.message_id)
    await m.answer(f"Получателей: <b>{n}</b>",
                   reply_markup=kb([[ib(f"✅ Отправить всем ({n})", f"abcgo:{m.chat.id}:{m.message_id}")],
                                    [ib("❌ Отмена", "a")]]))


@router.message(AdminSt.ban)
async def admin_ban_in(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    if m.text in (BTN_BOOK, BTN_MY, BTN_CONTACT, BTN_ADMIN):
        await state.clear()
        return await route_text(m, state)
    await state.clear()
    t = (m.text or "").strip().lstrip("@")
    u = None
    if t.isdigit():
        u = q("SELECT * FROM users WHERE user_id=?", (int(t),), one=True)
        if not u:
            ex("INSERT OR IGNORE INTO users(user_id, username, full_name, first_ts) VALUES(?,?,?,?)",
               (int(t), None, None, int(_time.time())))
            u = q("SELECT * FROM users WHERE user_id=?", (int(t),), one=True)
    elif t:
        u = q("SELECT * FROM users WHERE lower(username)=lower(?)", (t,), one=True)
    if not u:
        return await m.answer("Пользователь не найден (он должен хоть раз запустить бота). Пришлите @username или ID.",
                              reply_markup=kb([[ib("⬅️ Баны", "abl")]]))
    set_ban(u["user_id"], True)
    tx, k = user_card(u["user_id"])
    await m.answer("🚫 Забанен.\n\n" + tx, reply_markup=k)


@router.message(Shot.wait, F.photo | F.document)
async def shot_in_state(m: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await handle_shot(m, data.get("bid"))


@router.message(Shot.wait)
async def shot_wrong(m: Message, state: FSMContext):
    if m.text in (BTN_BOOK, BTN_MY, BTN_CONTACT, BTN_ADMIN):
        await state.clear()
        return await route_text(m, state)
    await state.clear()
    await fallback(m, state)


async def route_text(m, state):
    if m.text == BTN_BOOK:
        return await book(m, state)
    if m.text == BTN_MY:
        return await my(m, state)
    if m.text == BTN_CONTACT:
        return await contact(m)
    if m.text == BTN_ADMIN:
        return await admin_entry(m, state)


@router.message(F.photo | F.document)
async def photo_any(m: Message):
    bs = q("SELECT * FROM bookings WHERE user_id=? AND status IN ('hold','review')", (m.from_user.id,))
    if len(bs) == 1:
        return await handle_shot(m, bs[0]["id"])
    if not bs:
        await m.answer("У вас нет броней, ожидающих оплаты.")
    else:
        await m.answer("Откройте «📋 Брони», выберите бронь и нажмите «✅ Я оплатил».")


# ───────────────────────── admin views ─────────────────────────
def is_admin(uid):
    return uid == ADMIN_ID


def admin_menu():
    n = q("SELECT COUNT(*) n FROM bookings WHERE status='review'", one=True)["n"]
    return kb([
        [ib("📅 Расписание", "ac:now")],
        [ib("🗓 Брони на сегодня", "at"), ib("📋 Все брони", "al")],
        [ib(f"🔎 Оплаты ({n})", "ar")],
        [ib("👥 Пользователи", "au:0"), ib("📊 Статистика", "ast")],
        [ib("📢 Рассылка", "abc"), ib("🚫 Баны", "abl")],
    ])


def is_banned(uid):
    r = q("SELECT banned FROM users WHERE user_id=?", (uid,), one=True)
    return bool(r and r["banned"])


def set_ban(uid, val):
    ex("UPDATE users SET banned=? WHERE user_id=?", (1 if val else 0, uid))
    if val:
        ex("UPDATE bookings SET status='cancelled' WHERE user_id=? AND status IN ('hold','review')", (uid,))


def uname(u):
    return f"@{u['username']}" if u["username"] else (u["full_name"] or str(u["user_id"]))


def admin_today():
    t = today_admin()
    bs = active_between(local_to_utc(t, 0), local_to_utc(t, 24 * 60))
    title = f"🗓 <b>Брони на сегодня</b> ({t:%d.%m}, {TZ_ADMIN_LABEL})"
    rows = [[ib(f"{u2(b['start_utc']).astimezone(ATZ):%H:%M} {b['username'] or b['full_name']} · {STATUS[b['status']]}",
                f"ab:{b['id']}")] for b in bs]
    rows.append([ib("⬅️ Меню", "a")])
    return title + ("" if bs else "\n\nНа сегодня броней нет."), kb(rows)


PAGE = 10


def users_page(page):
    total = q("SELECT COUNT(*) n FROM users", one=True)["n"]
    us = q("""SELECT u.*, (SELECT COUNT(*) FROM bookings b WHERE b.user_id=u.user_id) nb,
              (SELECT COUNT(*) FROM bookings b WHERE b.user_id=u.user_id AND b.status='paid') np
              FROM users u ORDER BY u.first_ts DESC LIMIT ? OFFSET ?""", (PAGE, page * PAGE))
    rows = []
    for u in us:
        mark = "🚫 " if u["banned"] else ""
        info = f"{u['nb']} брон." + (f", {u['np']} оплач." if u["np"] else "") if u["nb"] else "без броней"
        rows.append([ib(f"{mark}{uname(u)} · {info}", f"auu:{u['user_id']}")])
    nav = []
    if page > 0:
        nav.append(ib("◀️", f"au:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(ib("▶️", f"au:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([ib("⬅️ Меню", "a")])
    pages = max(1, math.ceil(total / PAGE))
    return f"👥 <b>Пользователи</b>: {total} (стр. {page + 1}/{pages})\nНовые сверху.", kb(rows)


def user_card(uid):
    u = q("SELECT * FROM users WHERE user_id=?", (uid,), one=True)
    if not u:
        return "Пользователь не найден.", kb([[ib("⬅️ Пользователи", "au:0")]])
    bs = q("SELECT * FROM bookings WHERE user_id=? ORDER BY start_utc DESC LIMIT 15", (uid,))
    cnt = {}
    for b in q("SELECT status, COUNT(*) n FROM bookings WHERE user_id=? GROUP BY status", (uid,)):
        cnt[b["status"]] = b["n"]
    first = datetime.fromtimestamp(u["first_ts"] or 0, ATZ)
    lines = [f"👤 <b>{esc(uname(u))}</b> {('🚫 ЗАБАНЕН' if u['banned'] else '')}",
             f"Имя: {esc(u['full_name'])}", f"ID: <code>{uid}</code>", f"В боте с {first:%d.%m.%Y}",
             f"\nБроней всего: <b>{sum(cnt.values())}</b> · оплачено {cnt.get('paid', 0)} · "
             f"отменено {cnt.get('cancelled', 0)} · не оплачено {cnt.get('expired', 0) + cnt.get('rejected', 0)}"]
    names = {"cancelled": "❌ отменена", "expired": "⌛ не оплачена", "rejected": "❌ отклонена"}
    if bs:
        lines.append("\n<b>История:</b>")
        for b in bs:
            lines.append(f"• {fmt_admin(b['start_utc'])} — {STATUS.get(b['status']) or names.get(b['status'], b['status'])}")
    rows = [[ib("✅ Разбанить" if u["banned"] else "🚫 Забанить", f"{'aunban' if u['banned'] else 'aban'}:{uid}")]]
    if u["username"]:
        rows.append([ib("✉️ Написать", url=f"https://t.me/{u['username']}")])
    rows.append([ib("⬅️ Пользователи", "au:0")])
    return "\n".join(lines), kb(rows)


def bans_view():
    us = q("SELECT * FROM users WHERE banned=1 ORDER BY first_ts DESC")
    rows = [[ib(f"✅ Разбанить {uname(u)}", f"aunban:{u['user_id']}")] for u in us]
    rows.append([ib("➕ Забанить по @username / ID", "abadd")])
    rows.append([ib("⬅️ Меню", "a")])
    return f"🚫 <b>Баны</b>: {len(us)}" + ("" if us else "\n\nНикто не забанен."), kb(rows)


def stats_text():
    now = int(_time.time())
    t0 = int(datetime.combine(today_admin(), dtime(0), tzinfo=ATZ).timestamp())
    u_all = q("SELECT COUNT(*) n FROM users", one=True)["n"]
    u_today = q("SELECT COUNT(*) n FROM users WHERE first_ts>=?", (t0,), one=True)["n"]
    u_7 = q("SELECT COUNT(*) n FROM users WHERE first_ts>=?", (now - 7 * 86400,), one=True)["n"]
    u_ban = q("SELECT COUNT(*) n FROM users WHERE banned=1", one=True)["n"]
    u_book = q("SELECT COUNT(DISTINCT user_id) n FROM bookings", one=True)["n"]
    st = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM bookings GROUP BY status")}
    nowf = now_utc().strftime(FMT)
    done = q("SELECT COUNT(*) n FROM bookings WHERE status='paid' AND start_utc<?", (nowf,), one=True)["n"]
    ahead = q("SELECT COUNT(*) n FROM bookings WHERE status='paid' AND start_utc>=?", (nowf,), one=True)["n"]
    rev = q("SELECT COALESCE(SUM(price_rub),0) s FROM bookings WHERE status='paid'", one=True)["s"]
    m0 = today_admin().replace(day=1)
    rev_m = q("SELECT COALESCE(SUM(price_rub),0) s FROM bookings WHERE status='paid' AND start_utc>=?",
              (local_to_utc(m0, 0).strftime(FMT),), one=True)["s"]
    conv = f"{u_book / u_all * 100:.0f}%" if u_all else "—"
    return (f"📊 <b>Статистика</b>\n\n"
            f"👥 Пользователей: <b>{u_all}</b> (сегодня +{u_today}, за 7 дн. +{u_7})\n"
            f"🚫 Забанено: {u_ban}\n"
            f"📝 Хоть раз бронировали: {u_book} ({conv})\n\n"
            f"✅ Оплачено консультаций: <b>{st.get('paid', 0)}</b> (проведено {done}, впереди {ahead})\n"
            f"🔎 На проверке: {st.get('review', 0)} · ⏳ ждут оплаты: {st.get('hold', 0)}\n"
            f"❌ Отменено: {st.get('cancelled', 0)} · ⌛ не оплачено: {st.get('expired', 0) + st.get('rejected', 0)}\n\n"
            f"💰 Выручка всего: <b>{rev} ₽</b>\n"
            f"💰 В этом месяце: <b>{rev_m} ₽</b>")


def admin_cal(y, m):
    t = today_admin()
    mn = (t.year, t.month)
    mx = ((t + timedelta(days=365)).year, (t + timedelta(days=365)).month)
    first = date(y, m, 1)
    last = date(y, m, calendar.monthrange(y, m)[1])
    closed = {r["day"] for r in q("SELECT day FROM closed_days WHERE day BETWEEN ? AND ?",
                                  (first.isoformat(), last.isoformat()))}
    partial = {r["day"] for r in q("SELECT DISTINCT day FROM closed_hours WHERE day BETWEEN ? AND ?",
                                   (first.isoformat(), last.isoformat()))}
    booked = set()
    for b in active_between(local_to_utc(first, 0), local_to_utc(last, 24 * 60)):
        booked.add(u2(b["start_utc"]).astimezone(ATZ).date().isoformat())

    def lab(d):
        if d < t:
            return ib("·", "x")
        ds = d.isoformat()
        mark = "⛔" if ds in closed else ("🔹" if ds in booked else ("◐" if ds in partial else ""))
        return ib(f"{mark}{d.day}", f"ad:{ds}")
    rows = month_grid(y, m, lab, "ac", mn, mx)
    rows.append([ib("⛔ Закрыть месяц", f"amc:{y}-{m:02d}:1"), ib("✅ Открыть месяц", f"amc:{y}-{m:02d}:0")])
    rows.append([ib("⬅️ Меню", "a")])
    text = (f"📅 <b>Расписание</b> (время: {TZ_ADMIN_LABEL})\n"
            f"Нажми на день, чтобы закрыть часы или весь день.\n"
            f"⛔ закрыт · ◐ часть часов закрыта · 🔹 есть брони")
    return text, kb(rows)


def admin_day(ds):
    d = date.fromisoformat(ds)
    closed = day_closed(ds)
    ch = closed_hours(ds)
    bs = active_between(local_to_utc(d, 0), local_to_utc(d, 24 * 60))
    lines = [f"📅 <b>{d:%d.%m.%Y} ({WD[d.weekday()]})</b> — {'⛔ день закрыт' if closed else '✅ день открыт'}",
             f"Часы ({TZ_ADMIN_LABEL}): ✅ открыт / ⛔ закрыт"]
    if bs:
        lines.append("\n<b>Брони:</b>")
        for b in bs:
            lines.append(f"• {fmt_admin(b['start_utc'])} — {who(b)} {STATUS[b['status']]}")
    rows, row = [], []
    for h in WORK_HOURS:
        row.append(ib(f"{'⛔' if h in ch else '✅'} {h:02d}:00", f"ah:{ds}:{h}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([ib("✅ Открыть день" if closed else "⛔ Закрыть весь день", f"adc:{ds}")])
    for b in bs:
        rows.append([ib(f"👤 {u2(b['start_utc']).astimezone(ATZ):%H:%M} {b['username'] or b['full_name']}", f"ab:{b['id']}")])
    rows.append([ib("⬅️ К месяцу", f"ac:{d.year}-{d.month:02d}")])
    return "\n".join(lines), kb(rows)


def admin_list(review_only=False):
    if review_only:
        bs = q("SELECT * FROM bookings WHERE status='review' ORDER BY start_utc")
        title = "🔎 <b>Оплаты</b> (ждут подтверждения)"
    else:
        lo = (now_utc() - timedelta(minutes=DURATION)).strftime(FMT)
        bs = q(f"SELECT * FROM bookings WHERE status IN {ACTIVE} AND start_utc>=? ORDER BY start_utc LIMIT 40", (lo,))
        title = "📋 <b>Все брони</b> (предстоящие)"
    if not bs:
        return title + "\n\nПусто.", kb([[ib("⬅️ Меню", "a")]])
    rows = [[ib(f"{u2(b['start_utc']).astimezone(ATZ):%d.%m %H:%M} {b['username'] or b['full_name']} · "
                f"{STATUS[b['status']]}", f"ab:{b['id']}")] for b in bs]
    rows.append([ib("⬅️ Меню", "a")])
    return title, kb(rows)


def admin_booking(b):
    t = (f"🧾 <b>Бронь {b['code']}</b>\n👤 {who(b)} ({esc(b['full_name'])}, id <code>{b['user_id']}</code>)\n"
         f"📅 {fmt_admin(b['start_utc'])}\n{STATUS.get(b['status'], b['status'])}\n"
         f"💰 {b['price_rub']} ₽ / {b['price_usd']} $ / {b['price_eur']} €")
    if b["pay_note"]:
        t += f"\nОплата: {esc(b['pay_note'].strip())}"
    rows = []
    if b["status"] in ("hold", "review"):
        rows.append([ib("✅ Подтвердить оплату", f"aok:{b['id']}")])
    if b["status"] in ACTIVE:
        rows.append([ib("❌ Отменить бронь", f"ax:{b['id']}")])
    if b["username"]:
        rows.append([ib("✉️ Написать клиенту", url=f"https://t.me/{b['username']}")])
    rows.append([ib("👤 Профиль клиента", f"auu:{b['user_id']}")])
    rows.append([ib("⬅️ Список", "al")])
    return t, kb(rows)


@router.message(F.text == BTN_ADMIN)
@router.message(Command("admin"))
async def admin_entry(m: Message, state: FSMContext):
    await state.clear()
    if not is_admin(m.from_user.id):
        return
    await m.answer("🔒 <b>Админка</b>", reply_markup=admin_menu())


# ───────────────────────── callbacks ─────────────────────────
async def edit(cq: CallbackQuery, text, markup=None):
    try:
        if cq.message.photo or cq.message.document:
            await cq.message.edit_caption(caption=text, reply_markup=markup)
        else:
            await cq.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        if "not modified" not in str(e):
            await cq.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


ADMIN_CMDS = {"a", "ac", "ad", "ah", "adc", "amc", "al", "ar", "ab", "aok", "ano", "ax", "axy",
              "at", "au", "auu", "aban", "aunban", "abl", "abadd", "abc", "abcgo", "ast"}


@router.callback_query()
async def cb(cq: CallbackQuery, state: FSMContext):
    bot = cq.bot
    uid = cq.from_user.id
    parts = (cq.data or "x").split(":")
    cmd, args = parts[0], parts[1:]
    if cmd == "x":
        return await cq.answer()
    if cmd in ADMIN_CMDS and not is_admin(uid):
        return await cq.answer("Нет доступа", show_alert=True)

    # ── client ──
    if cmd == "newbook":
        await cq.answer()
        t = today_admin()
        return await edit(cq, f"📅 Выберите день (консультация 60 мин, {await price_line()}):", client_cal(t.year, t.month, 0))

    if cmd == "cc":  # cc:rid:YYYY-MM
        rid = int(args[0])
        y, m = parse_ym(args[1])
        await cq.answer()
        return await cq.message.edit_reply_markup(reply_markup=client_cal(y, m, rid))

    if cmd == "cd":  # cd:date:rid
        d, rid = date.fromisoformat(args[0]), int(args[1])
        slots = free_slots(d, rid or None) if bookable_day(d) else []
        if not slots:
            return await cq.answer("На этот день свободного времени нет", show_alert=True)
        await cq.answer()
        rows, row = [], []
        for s in slots:
            t = s.replace(tzinfo=UTC).astimezone(CTZ)
            row.append(ib(t.strftime("%H:%M"), f"ct:{s:%Y%m%d%H%M}:{rid}"))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([ib("⬅️ Другой день", f"cc:{rid}:{d.year}-{d.month:02d}")])
        return await edit(cq, f"🕒 {d:%d.%m} ({WD[d.weekday()]}) — выберите время начала ({TZ_CLIENT_LABEL}):", kb(rows))

    if cmd in ("ct", "cy"):  # ct:YYYYmmddHHMM:rid
        s = datetime.strptime(args[0], "%Y%m%d%H%M")
        rid = int(args[1])
        d = s.replace(tzinfo=UTC).astimezone(ATZ).date()
        if s not in free_slots(d, rid or None):
            return await cq.answer("Это время уже занято, выберите другое", show_alert=True)
        sf = s.strftime(FMT)
        if cmd == "ct":
            await cq.answer()
            act = "Перенести на" if rid else "Записаться на"
            return await edit(cq, f"{act} <b>{fmt_client(sf)}</b>?\n⏱ 60 минут, звонок в Telegram\n💰 {await price_line()}",
                              kb([[ib("✅ Да", f"cy:{args[0]}:{rid}")],
                                  [ib("⬅️ Другое время", f"cd:{d.isoformat()}:{rid}")]]))
        # confirm
        if rid:
            b = get_b(rid)
            if not b or b["user_id"] != uid or b["status"] not in ACTIVE:
                return await cq.answer("Бронь неактивна", show_alert=True)
            old = b["start_utc"]
            ex("UPDATE bookings SET start_utc=?, rem24=0, rem1=0 WHERE id=?", (sf, rid))
            await cq.answer("Перенесено ✅")
            await edit(cq, f"🔁 Бронь перенесена на <b>{fmt_client(sf)}</b>.\nСтатус: {STATUS[b['status']]}")
            return await to_admin(bot, f"🔁 {who(b)} перенёс консультацию\nс {fmt_admin(old)}\nна <b>{fmt_admin(sf)}</b>\n"
                                       f"Статус: {STATUS[b['status']]}")
        if q("SELECT 1 FROM bookings WHERE user_id=? AND status='hold'", (uid,), one=True):
            return await cq.answer("Сначала оплатите или отмените текущую бронь", show_alert=True)
        usd, eur = await prices()
        while True:
            code = f"Z{random.randint(10000, 99999)}"
            if not q("SELECT 1 FROM bookings WHERE code=?", (code,), one=True):
                break
        u = cq.from_user
        save_user(u)
        bid = ex("INSERT INTO bookings(code,user_id,username,full_name,start_utc,status,created_ts,hold_until_ts,"
                 "price_rub,price_usd,price_eur) VALUES(?,?,?,?,?,'hold',?,?,?,?,?)",
                 (code, uid, u.username, u.full_name, sf, int(_time.time()), int(_time.time()) + HOLD_MIN * 60,
                  PRICE_RUB, usd, eur))
        b = get_b(bid)
        await cq.answer("Забронировано ✅")
        await edit(cq, f"✅ Время <b>{fmt_client(sf)}</b> забронировано за вами.")
        await cq.message.answer(await pay_text(b), reply_markup=await pay_kb(b), disable_web_page_preview=True)
        return await to_admin(bot, f"🆕 {who(b)} записался на <b>{fmt_admin(sf)}</b>\n"
                                   f"Код {code} · ⏳ ждёт оплаты ({HOLD_MIN // 60} ч)",
                              kb([[ib("Открыть", f"ab:{bid}")]]))

    if cmd == "mbl":
        await cq.answer()
        t, k = my_view(uid)
        return await edit(cq, t, k)

    if cmd in ("mb", "mbr", "mbx", "mbxy", "pay", "shot", "paid", "pm"):
        b = get_b(int(args[0]))
        if not b or b["user_id"] != uid or b["status"] not in ACTIVE:
            await cq.answer("Бронь неактивна", show_alert=True)
            t, k = my_view(uid)
            return await edit(cq, t, k)
        bid = b["id"]
        if cmd == "mb":
            await cq.answer()
            rows = []
            if b["status"] == "hold":
                rows.append([ib("💳 Оплатить", f"pay:{bid}")])
            rows += [[ib("🔁 Перенести", f"mbr:{bid}")], [ib("❌ Отменить", f"mbx:{bid}")], [ib("⬅️ Назад", "mbl")]]
            return await edit(cq, f"🧾 <b>Бронь {b['code']}</b>\n📅 {fmt_client(b['start_utc'])}\n{STATUS[b['status']]}", kb(rows))
        if cmd == "pay":
            await cq.answer()
            return await cq.message.answer(await pay_text(b), reply_markup=await pay_kb(b), disable_web_page_preview=True)
        if cmd == "mbr":
            await cq.answer()
            t = today_admin()
            return await edit(cq, f"🔁 Перенос брони {b['code']} ({fmt_client(b['start_utc'])})\nВыберите новый день:",
                              client_cal(t.year, t.month, bid))
        if cmd == "mbx":
            await cq.answer()
            extra = f"\n\nОплата уже внесена — по возврату напишите @{ADMIN_USERNAME}." if b["status"] == "paid" else ""
            return await cq.message.answer(f"Отменить бронь на <b>{fmt_client(b['start_utc'])}</b>?{extra}",
                                           reply_markup=kb([[ib("❌ Да, отменить", f"mbxy:{bid}")],
                                                            [ib("🔁 Лучше перенести", f"mbr:{bid}")]]))
        if cmd == "mbxy":
            ex("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
            await cq.answer("Отменено")
            await edit(cq, f"❌ Бронь на {fmt_client(b['start_utc'])} отменена.")
            return await to_admin(bot, f"❌ {who(b)} отменил бронь на {fmt_admin(b['start_utc'])}\n"
                                       f"Была: {STATUS[b['status']]}")
        if cmd == "paid":
            await cq.answer()
            return await cq.message.answer(
                "Чем вы оплатили?",
                reply_markup=kb([[ib(v, f"pm:{bid}:{k}")] for k, v in PAY_METHODS.items()]))
        if cmd == "pm":
            method = PAY_METHODS.get(args[1], args[1])
            if b["status"] not in ("hold", "review"):
                return await cq.answer("Бронь уже оплачена", show_alert=True)
            ex("UPDATE bookings SET status='review', pay_note=COALESCE(pay_note,'')||? WHERE id=?",
               (f" клиент: {method}", bid))
            await cq.answer()
            await edit(cq, f"👍 Спасибо! Оплата ({method}) отмечена — проверю поступление и подтвержу бронь, "
                           f"вам придёт сообщение.\n\nЕсли есть скрин — можете прислать его сюда (необязательно).")
            await state.set_state(Shot.wait)
            await state.update_data(bid=bid)
            return await to_admin(bot, f"💳 {who(b)} отметил оплату: <b>{method}</b>\n"
                                       f"Бронь {b['code']}: {fmt_admin(b['start_utc'])}\n"
                                       f"Сумма: {b['price_rub']} ₽ / {b['price_usd']} $ / {b['price_eur']} €\n"
                                       f"Проверь поступление 👇",
                                  kb([[ib("✅ Подтвердить", f"aok:{bid}"), ib("❌ Отклонить", f"ano:{bid}")]]))
        if cmd == "shot":
            await cq.answer()
            await state.set_state(Shot.wait)
            await state.update_data(bid=bid)
            return await cq.message.answer("📸 Пришлите скриншот оплаты (картинкой или файлом).")

    # ── admin ──
    if cmd == "a":
        await cq.answer()
        await state.clear()
        return await edit(cq, "🔒 <b>Админка</b>", admin_menu())
    if cmd == "ac":
        await cq.answer()
        if args[0] == "now":
            t = today_admin()
            y, m = t.year, t.month
        else:
            y, m = parse_ym(args[0])
        t, k = admin_cal(y, m)
        return await edit(cq, t, k)
    if cmd == "ad":
        await cq.answer()
        t, k = admin_day(args[0])
        return await edit(cq, t, k)
    if cmd == "ah":
        ds, h = args[0], int(args[1])
        if h in closed_hours(ds):
            ex("DELETE FROM closed_hours WHERE day=? AND hour=?", (ds, h))
        else:
            ex("INSERT OR IGNORE INTO closed_hours(day,hour) VALUES(?,?)", (ds, h))
        await cq.answer()
        t, k = admin_day(ds)
        return await edit(cq, t, k)
    if cmd == "adc":
        ds = args[0]
        if day_closed(ds):
            ex("DELETE FROM closed_days WHERE day=?", (ds,))
            await cq.answer("День открыт")
        else:
            ex("INSERT OR IGNORE INTO closed_days(day) VALUES(?)", (ds,))
            await cq.answer("День закрыт")
        t, k = admin_day(ds)
        return await edit(cq, t, k)
    if cmd == "amc":
        y, m = parse_ym(args[0])
        close = args[1] == "1"
        first = date(y, m, 1)
        last = date(y, m, calendar.monthrange(y, m)[1])
        if close:
            d = max(first, today_admin())
            while d <= last:
                ex("INSERT OR IGNORE INTO closed_days(day) VALUES(?)", (d.isoformat(),))
                d += timedelta(days=1)
        else:
            ex("DELETE FROM closed_days WHERE day BETWEEN ? AND ?", (first.isoformat(), last.isoformat()))
            ex("DELETE FROM closed_hours WHERE day BETWEEN ? AND ?", (first.isoformat(), last.isoformat()))
        await cq.answer("Месяц закрыт" if close else "Месяц открыт")
        t, k = admin_cal(y, m)
        return await edit(cq, t, k)
    if cmd == "at":
        await cq.answer()
        t, k = admin_today()
        return await edit(cq, t, k)
    if cmd == "au":
        await cq.answer()
        t, k = users_page(int(args[0]))
        return await edit(cq, t, k)
    if cmd == "auu":
        await cq.answer()
        t, k = user_card(int(args[0]))
        return await edit(cq, t, k)
    if cmd in ("aban", "aunban"):
        tuid = int(args[0])
        if tuid == ADMIN_ID:
            return await cq.answer("Себя нельзя", show_alert=True)
        set_ban(tuid, cmd == "aban")
        await cq.answer("Забанен 🚫" if cmd == "aban" else "Разбанен ✅")
        t, k = user_card(tuid)
        return await edit(cq, t, k)
    if cmd == "abl":
        await cq.answer()
        await state.clear()
        t, k = bans_view()
        return await edit(cq, t, k)
    if cmd == "abadd":
        await cq.answer()
        await state.set_state(AdminSt.ban)
        return await edit(cq, "Пришлите @username или ID пользователя для бана:", kb([[ib("❌ Отмена", "abl")]]))
    if cmd == "ast":
        await cq.answer()
        return await edit(cq, stats_text(), kb([[ib("🔄 Обновить", "ast")], [ib("⬅️ Меню", "a")]]))
    if cmd == "abc":
        await cq.answer()
        await state.set_state(AdminSt.bcast)
        return await edit(cq, "📢 Пришлите сообщение для рассылки (текст, фото, видео — что угодно).",
                          kb([[ib("❌ Отмена", "a")]]))
    if cmd == "abcgo":
        await cq.answer("Рассылка запущена")
        await edit(cq, "📢 Рассылка запущена, по окончании пришлю отчёт.")
        asyncio.create_task(broadcast(bot, int(args[0]), int(args[1])))
        return
    if cmd in ("al", "ar"):
        await cq.answer()
        t, k = admin_list(cmd == "ar")
        return await edit(cq, t, k)
    if cmd in ("ab", "aok", "ano", "ax", "axy"):
        b = get_b(int(args[0]))
        if not b:
            return await cq.answer("Не найдено", show_alert=True)
        bid = b["id"]
        if cmd == "ab":
            await cq.answer()
            t, k = admin_booking(b)
            return await edit(cq, t, k)
        if cmd == "aok":
            ok = await mark_paid(bot, bid, "подтверждено вручную")
            await cq.answer("Оплата подтверждена ✅" if ok else "Бронь уже не ждёт оплаты", show_alert=not ok)
            t, k = admin_booking(get_b(bid))
            return await edit(cq, t, k)
        if cmd == "ano":
            if b["status"] not in ("hold", "review"):
                return await cq.answer("Бронь уже не ждёт оплаты", show_alert=True)
            ex("UPDATE bookings SET status='rejected' WHERE id=?", (bid,))
            await to_user(bot, b["user_id"],
                          f"❌ Оплата брони {b['code']} ({fmt_client(b['start_utc'])}) не найдена, бронь снята.\n"
                          f"Если это ошибка — напишите @{ADMIN_USERNAME}.")
            await cq.answer("Отклонено")
            t, k = admin_booking(get_b(bid))
            return await edit(cq, t, k)
        if cmd == "ax":
            await cq.answer()
            return await edit(cq, f"Отменить бронь {who(b)} на {fmt_admin(b['start_utc'])}? Клиент получит уведомление.",
                              kb([[ib("❌ Да, отменить", f"axy:{bid}")], [ib("⬅️ Назад", f"ab:{bid}")]]))
        if cmd == "axy":
            if b["status"] in ACTIVE:
                ex("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
                await to_user(bot, b["user_id"],
                              f"❌ Консультация {fmt_client(b['start_utc'])} отменена.\n"
                              f"Напишите @{ADMIN_USERNAME}, чтобы подобрать другое время"
                              + (" или оформить возврат." if b["status"] == "paid" else "."))
            await cq.answer("Отменено")
            t, k = admin_list()
            return await edit(cq, t, k)
    await cq.answer()


@router.message()
async def fallback(m: Message, state: FSMContext):
    await m.answer("Выберите действие кнопками ниже 👇", reply_markup=main_kb(m.from_user.id))


# ───────────────────────── background ─────────────────────────
async def expire_holds(bot):
    for b in q("SELECT * FROM bookings WHERE status='hold' AND hold_until_ts<?", (int(_time.time()),)):
        ex("UPDATE bookings SET status='expired' WHERE id=? AND status='hold'", (b["id"],))
        await to_user(bot, b["user_id"],
                      f"⌛ Время на оплату брони {b['code']} ({fmt_client(b['start_utc'])}) истекло, время освобождено.\n"
                      f"Если уже оплатили — напишите @{ADMIN_USERNAME}.")
        await to_admin(bot, f"⌛ Бронь {who(b)} на {fmt_admin(b['start_utc'])} снята — не оплачена.")


async def reminders(bot):
    now = now_utc()
    lo = (now - timedelta(minutes=5)).strftime(FMT)
    hi = (now + timedelta(hours=25)).strftime(FMT)
    for b in q("SELECT * FROM bookings WHERE status='paid' AND start_utc BETWEEN ? AND ?", (lo, hi)):
        delta = (datetime.strptime(b["start_utc"], FMT) - now).total_seconds() / 60
        if not b["rem24"] and delta <= 24 * 60:
            ex("UPDATE bookings SET rem24=1 WHERE id=?", (b["id"],))
            if delta > 70:
                await to_user(bot, b["user_id"], f"🔔 Напоминание: завтра консультация — <b>{fmt_client(b['start_utc'])}</b>.\n"
                                                 f"Я позвоню вам в Telegram. Если нужно перенести — «📋 Брони».")
        if not b["rem1"] and delta <= 60:
            ex("UPDATE bookings SET rem1=1 WHERE id=?", (b["id"],))
            if delta > 0:
                await to_user(bot, b["user_id"], f"⏰ Через час консультация — <b>{fmt_client(b['start_utc'])}</b>.\n"
                                                 f"Будьте на связи в Telegram, я позвоню.")
                await to_admin(bot, f"⏰ Через час консультация с {who(b)} — {fmt_admin(b['start_utc'])}")


def _contains_code(item, code):
    s = json.dumps(item, ensure_ascii=False)
    if code in s:
        return True
    cb = code.encode()
    for key in ("forward_payload", "body"):
        stack = [item]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                for k, v in x.items():
                    if k == key and isinstance(v, str):
                        try:
                            if cb in base64.b64decode(v):
                                return True
                        except Exception:
                            pass
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(x, list):
                stack.extend(x)
    return False


def _hash(item):
    return item.get("transaction_hash") or item.get("hash") or json.dumps(item)[:200]


async def check_chain(bot):
    pend = q("SELECT * FROM bookings WHERE status IN ('hold','review')")
    if not pend:
        return
    hdr = {"X-API-Key": TONCENTER_KEY} if TONCENTER_KEY else None
    min_ts = min(b["created_ts"] for b in pend) - 600
    found = []  # (booking, kind, amount, hash)
    try:
        j = await http_json(f"{TONCENTER}/jetton/transfers",
                            {"owner_address": TON_ADDR, "direction": "in", "limit": 50, "sort": "desc"}, hdr)
        for it in j.get("jetton_transfers", []):
            if it.get("transaction_aborted") or (it.get("jetton_master") or "").upper() != USDT_MASTER_RAW:
                continue
            if int(it.get("transaction_now") or 0) < min_ts:
                continue
            for b in pend:
                if _contains_code(it, b["code"]):
                    found.append((b, "USDT", int(it["amount"]) / 1e6, _hash(it)))
    except Exception:
        log.exception("jetton check")
    await asyncio.sleep(1.5)
    try:
        j = await http_json(f"{TONCENTER}/transactions", {"account": TON_ADDR, "limit": 50, "sort": "desc"}, hdr)
        for tx in j.get("transactions", []):
            im = tx.get("in_msg") or {}
            if not im.get("source") or int(tx.get("now") or 0) < min_ts:
                continue
            val = int(im.get("value") or 0) / 1e9
            if val < 0.05:
                continue  # служебные сообщения jetton-кошелька
            for b in pend:
                if _contains_code(im, b["code"]):
                    found.append((b, "GRAM", val, _hash(tx)))
    except Exception:
        log.exception("ton check")

    for b, kind, amount, h in found:
        if q("SELECT 1 FROM chain_seen WHERE h=?", (h,), one=True):
            continue
        ex("INSERT OR IGNORE INTO chain_seen(h) VALUES(?)", (h,))
        usd_val = amount if kind == "USDT" else (amount * (await ton_price() or 0))
        note = f"{kind} {amount:g} (≈{usd_val:.2f}$)"
        if usd_val >= b["price_usd"] * 0.95:
            if await mark_paid(bot, b["id"], f"авто: {note}"):
                await to_admin(bot, f"💰 Автооплата {note} от {who(b)}\nБронь {b['code']}: {fmt_admin(b['start_utc'])} ✅")
        else:
            ex("UPDATE bookings SET pay_note=COALESCE(pay_note,'')||? WHERE id=?", (f" пришло {note}", b["id"]))
            await to_admin(bot, f"⚠️ По брони {b['code']} ({who(b)}) пришло {note}, нужно {b['price_usd']}$. Проверь.",
                           kb([[ib("✅ Подтвердить", f"aok:{b['id']}"), ib("❌ Отклонить", f"ano:{b['id']}")]]))


async def worker(bot):
    last_chain = 0
    while True:
        try:
            await expire_holds(bot)
            await reminders(bot)
            if _time.time() - last_chain > 60:
                last_chain = _time.time()
                await check_chain(bot)
        except Exception:
            log.exception("worker")
        await asyncio.sleep(30)


async def broadcast(bot, chat_id, msg_id):
    ok = fail = 0
    for u in q("SELECT user_id FROM users WHERE COALESCE(banned,0)=0 AND user_id!=?", (ADMIN_ID,)):
        try:
            await bot.copy_message(u["user_id"], chat_id, msg_id)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await to_admin(bot, f"📢 Рассылка завершена: доставлено <b>{ok}</b>, не доставлено {fail} (заблокировали бота).")


async def ban_mw(handler, event, data):
    u = getattr(event, "from_user", None)
    if u and u.id != ADMIN_ID and is_banned(u.id):
        if isinstance(event, CallbackQuery):
            await event.answer("Доступ ограничен", show_alert=True)
        return
    return await handler(event, data)


async def main():
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.message.outer_middleware(ban_mw)
    dp.callback_query.outer_middleware(ban_mw)
    dp.include_router(router)
    asyncio.create_task(worker(bot))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
