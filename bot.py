#!/usr/bin/env python3
"""
Telegram-бот: графік відключень світла для черги 3.2
(Софіївська Борщагівка, Київська область).

Дані: https://github.com/Baskerville42/outage-data-ua  (файл data/kyiv-region.json)

Що вміє:
  • показує, чи є світло зараз і коли воно зміниться;
  • графік на сьогодні та на завтра;
  • нагадує за кілька хвилин до відключення і коли світло має з'явитися;
  • повідомляє, якщо графік змінився.
"""
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from telegram import ReplyKeyboardMarkup, Update
from telegram.error import Forbidden
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

# ───────────────────────── Налаштування ─────────────────────────
BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Читає файл .env поруч із bot.py (рядки виду КЛЮЧ=значення)."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

GROUP = os.environ.get("GROUP", "GPV3.2")  # черга 3.2
DATA_URL = os.environ.get(
    "DATA_URL",
    "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv-region.json",
)
REMIND_MIN = int(os.environ.get("REMIND_MIN", "15"))  # за скільки хвилин нагадувати
CACHE_SECONDS = 300  # як часто тягнути дані з GitHub
STATE_FILE = BASE_DIR / "state.json"
TZ = ZoneInfo("Europe/Kyiv")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("outage-bot")

# ───────────────────────── Логіка графіка ─────────────────────────
ON, OFF, MAYBE = "on", "off", "maybe"

# значення з даних → (перші 30 хв години, другі 30 хв години)
HALF = {
    "yes": (ON, ON),
    "no": (OFF, OFF),
    "first": (OFF, ON),
    "second": (ON, OFF),
    "maybe": (MAYBE, MAYBE),
    "mfirst": (MAYBE, ON),
    "msecond": (ON, MAYBE),
}
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
SYMBOL = {ON: "1", OFF: "0", MAYBE: "?"}


def day_slots(hours: dict) -> list:
    """24 години → 48 півгодинних слотів."""
    slots = []
    for h in range(1, 25):
        slots += HALF.get(hours.get(str(h)), (MAYBE, MAYBE))
    return slots


def get_day(data: dict, d: date):
    """Графік на дату d: (48 слотів, джерело). Джерело: 'fact' | 'preset' | None."""
    for ts, groups in (data.get("fact", {}).get("data") or {}).items():
        if datetime.fromtimestamp(int(ts), TZ).date() == d and GROUP in groups:
            return day_slots(groups[GROUP]), "fact"
    hours = ((data.get("preset", {}).get("data") or {}).get(GROUP) or {}).get(str(d.isoweekday()))
    if hours:
        return day_slots(hours), "preset"
    return None, None


def segments(slots: list) -> list:
    """Проміжки без світла / можливих відключень: [(початок, кінець, тип)] у слотах."""
    out, i, n = [], 0, len(slots)
    while i < n:
        s = slots[i]
        if s not in (OFF, MAYBE):
            i += 1
            continue
        j = i
        while j < n and slots[j] == s:
            j += 1
        out.append((i, j, s))
        i = j
    return out


def fmt_slot(i: int) -> str:
    m = i * 30
    return f"{m // 60:02d}:{m % 60:02d}"


def fmt_hours(slots_count: int) -> str:
    h = slots_count / 2
    return f"{int(h)} год" if h == int(h) else f"{h:.1f} год".replace(".", ",")


def fmt_delta(td: timedelta) -> str:
    minutes = max(int(td.total_seconds() // 60), 0)
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} год {m} хв"
    return f"{h} год" if h else f"{m} хв"


def day_title(d: date) -> str:
    return f"{DAYS[d.weekday()]} {d:%d.%m}"


def day_text(data: dict, d: date, label: str) -> str:
    slots, src = get_day(data, d)
    head = f"📅 {label}, {day_title(d)} · {GROUP.replace('GPV', 'черга ')}"
    if slots is None:
        return f"{head}\n\nДаних немає."
    segs = segments(slots)
    lines = [head, ""]
    if not segs:
        lines.append("✅ Відключень не заплановано")
    for i, j, s in segs:
        icon, word = ("🔴", "") if s == OFF else ("🟡", " (можливо)")
        lines.append(f"{icon} {fmt_slot(i)}–{fmt_slot(j)}{word} · {fmt_hours(j - i)}")
    off = sum(j - i for i, j, s in segs if s == OFF)
    maybe = sum(j - i for i, j, s in segs if s == MAYBE)
    if off or maybe:
        lines.append("")
        if off:
            lines.append(f"Без світла: {fmt_hours(off)}")
        if maybe:
            lines.append(f"Можливі відключення: {fmt_hours(maybe)}")
    if src == "preset":
        lines += ["", "⚠️ Графік на цей день ще не опубліковано. Показано тижневий шаблон — він орієнтовний."]
    elif (data.get("fact") or {}).get("update"):
        lines += ["", f"Оновлено: {data['fact']['update']}"]
    return "\n".join(lines)


def build_timeline(data: dict, start: date, days: int = 2):
    tl = []
    for n in range(days):
        slots, _ = get_day(data, start + timedelta(days=n))
        tl += slots if slots else [None] * 48
    return tl


def now_text(data: dict) -> str:
    now = datetime.now(TZ)
    day0 = datetime(now.year, now.month, now.day, tzinfo=TZ)
    tl = build_timeline(data, now.date())
    idx = (now.hour * 60 + now.minute) // 30
    cur = tl[idx]
    if cur is None:
        return "Даних про поточний час немає."
    k = idx
    while k < len(tl) and tl[k] == cur:
        k += 1
    change = day0 + timedelta(minutes=30 * k) if k < len(tl) and tl[k] is not None else None
    until = f"{change:%H:%M} (через {fmt_delta(change - now)})" if change else None
    if cur == ON:
        if change:
            return f"🟢 Світло має бути.\nНаступна зміна — о {until}."
        return "🟢 Світло має бути.\nДо кінця відомого графіка відключень немає."
    if cur == OFF:
        if change:
            return f"🔴 За графіком світла немає.\nМає з'явитись о {until}."
        return "🔴 За графіком світла немає.\nЧас повернення ще невідомий (графік на завтра не опубліковано)."
    if change:
        return f"🟡 Зараз можливе відключення.\nСтатус зміниться о {until}."
    return "🟡 Зараз можливе відключення."


# ───────────────────────── Дані та стан ─────────────────────────
_cache = {"data": None, "ts": 0.0}


async def load_data():
    if _cache["data"] and time.time() - _cache["ts"] < CACHE_SECONDS:
        return _cache["data"]
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(DATA_URL)
            r.raise_for_status()
            _cache.update(data=r.json(), ts=time.time())
    except Exception as e:  # noqa: BLE001
        log.warning("Не вдалося завантажити дані: %s", e)
    return _cache["data"]  # якщо помилка — віддаємо старі дані (якщо є)


def load_state() -> dict:
    state = {"subs": [], "sent": {}, "sigs": {}}
    if STATE_FILE.exists():
        try:
            state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            log.warning("state.json пошкоджений, починаю з чистого")
    for cid in filter(None, os.environ.get("CHAT_IDS", "").split(",")):
        if int(cid) not in state["subs"]:
            state["subs"].append(int(cid))
    return state


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


STATE = load_state()

# ───────────────────────── Telegram ─────────────────────────
KEYBOARD = ReplyKeyboardMarkup(
    [["💡 Зараз", "📅 Сьогодні", "📅 Завтра"], ["🔔 Нагадування"]], resize_keyboard=True
)


async def reply_data(update: Update, builder) -> None:
    data = await load_data()
    if not data:
        await update.message.reply_text("Не вдалося отримати дані з GitHub. Спробуйте за хвилину.")
        return
    await update.message.reply_text(builder(data), reply_markup=KEYBOARD)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cid = update.effective_chat.id
    if cid not in STATE["subs"]:
        STATE["subs"].append(cid)
        save_state(STATE)
    await update.message.reply_text(
        f"Привіт! Я показую графік відключень світла для {GROUP.replace('GPV', 'черги ')} "
        "(Київська область).\n\n"
        f"🔔 Нагадування увімкнено: напишу за {REMIND_MIN} хв до відключення, коли має з'явитись світло "
        "і якщо графік зміниться. Вимкнути можна кнопкою «Нагадування».",
        reply_markup=KEYBOARD,
    )


async def cmd_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_data(update, now_text)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_data(update, lambda d: day_text(d, datetime.now(TZ).date(), "Сьогодні"))


async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply_data(update, lambda d: day_text(d, datetime.now(TZ).date() + timedelta(days=1), "Завтра"))


async def cmd_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cid = update.effective_chat.id
    if cid in STATE["subs"]:
        STATE["subs"].remove(cid)
        msg = "🔕 Нагадування вимкнено. Щоб увімкнути знову, натисніть «Нагадування»."
    else:
        STATE["subs"].append(cid)
        msg = "🔔 Нагадування увімкнено."
    save_state(STATE)
    await update.message.reply_text(msg, reply_markup=KEYBOARD)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Ваш chat id: {update.effective_chat.id}")


async def broadcast(bot, text: str) -> None:
    for cid in list(STATE["subs"]):
        try:
            await bot.send_message(cid, text)
        except Forbidden:  # користувач заблокував бота
            STATE["subs"].remove(cid)
            save_state(STATE)
        except Exception as e:  # noqa: BLE001
            log.warning("Не вдалося надіслати %s: %s", cid, e)


async def tick(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Щохвилини: нагадування та перевірка змін графіка."""
    data = await load_data()
    if not data:
        return
    now = datetime.now(TZ)
    today = now.date()
    day0 = datetime(now.year, now.month, now.day, tzinfo=TZ)
    changed_state = False

    # 1) графік змінився?
    for n in (0, 1):
        d = today + timedelta(days=n)
        slots, src = get_day(data, d)
        sig = f"{src}:" + "".join(SYMBOL[s] for s in slots) if slots else "none"
        old = STATE["sigs"].get(d.isoformat())
        if old != sig:
            STATE["sigs"][d.isoformat()] = sig
            changed_state = True
            if old is not None and sig != "none":
                await broadcast(ctx.bot, "🔄 Графік змінився!\n\n" + day_text(data, d, "Сьогодні" if n == 0 else "Завтра"))
    STATE["sigs"] = {k: v for k, v in STATE["sigs"].items() if k >= today.isoformat()}

    # 2) нагадування про відключення та повернення світла
    tl = build_timeline(data, today)
    for i, j, s in segments(tl):
        if s != OFF:
            continue
        start = day0 + timedelta(minutes=30 * i)
        end = day0 + timedelta(minutes=30 * j)
        if i > 0 and timedelta(0) < start - now <= timedelta(minutes=REMIND_MIN):
            key = f"pre:{start.isoformat()}"
            if key not in STATE["sent"]:
                STATE["sent"][key] = now.isoformat()
                changed_state = True
                await broadcast(
                    ctx.bot,
                    f"⚠️ Через {fmt_delta(start - now)} відключення світла\n"
                    f"🔴 {start:%H:%M}–{end:%H:%M} · {fmt_hours(j - i)}",
                )
        if j < len(tl) and tl[j] is not None and timedelta(0) <= now - end < timedelta(minutes=10):
            key = f"post:{end.isoformat()}"
            if key not in STATE["sent"]:
                STATE["sent"][key] = now.isoformat()
                changed_state = True
                await broadcast(ctx.bot, "💡 Світло має з'явитись зараз (за графіком).")

    # чистимо старі записи
    cutoff = now - timedelta(days=3)
    STATE["sent"] = {k: v for k, v in STATE["sent"].items() if datetime.fromisoformat(v) > cutoff}
    if changed_state:
        save_state(STATE)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задано BOT_TOKEN. Створіть файл .env поруч із bot.py і впишіть: BOT_TOKEN=ваш_токен")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler(["subscribe", "unsubscribe"], cmd_toggle))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.Regex(r"Зараз$"), cmd_now))
    app.add_handler(MessageHandler(filters.Regex(r"Сьогодні$"), cmd_today))
    app.add_handler(MessageHandler(filters.Regex(r"Завтра$"), cmd_tomorrow))
    app.add_handler(MessageHandler(filters.Regex(r"Нагадування$"), cmd_toggle))
    app.job_queue.run_repeating(tick, interval=60, first=15)
    log.info("Бот запущено. Черга: %s", GROUP)
    app.run_polling()


if __name__ == "__main__":
    main()
