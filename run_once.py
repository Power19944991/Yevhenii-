#!/usr/bin/env python3
"""
Одноразовий запуск бота для GitHub Actions (безкоштовний «хостинг»).

Кожен запуск (раз на ~5 хв):
  1) відповідає на нові повідомлення в боті (/start, Зараз, Сьогодні, Завтра, /id);
  2) надсилає нагадування та повідомлення про зміну графіка
     на chat id зі змінної CHAT_IDS.

Потрібні змінні: BOT_TOKEN, CHAT_IDS (через кому). Логіка графіка — у bot.py.
"""
import asyncio
import json
import os
import types

import httpx

import bot  # той самий bot.py: логіка графіка, нагадування, стан

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Не задано BOT_TOKEN")
API = f"https://api.telegram.org/bot{TOKEN}"

KEYBOARD = {
    "keyboard": [[{"text": "💡 Зараз"}, {"text": "📅 Сьогодні"}, {"text": "📅 Завтра"}]],
    "resize_keyboard": True,
}

# Список підписників (chat id) не записуємо у state.json — репозиторій публічний.
_orig_save = bot.save_state
bot.save_state = lambda state: _orig_save({**state, "subs": []})


class Api:
    """Мінімальна заміна telegram.Bot: потрібен лише send_message."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def send_message(self, chat_id, text, keyboard=False):
        payload = {"chat_id": chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = KEYBOARD
        r = await self.client.post(f"{API}/sendMessage", json=payload)
        if r.status_code == 403:
            raise bot.Forbidden("Користувач заблокував бота")
        r.raise_for_status()


HELP = (
    "Привіт! Я показую графік відключень світла для {group} (Київська область).\n\n"
    "Кнопки внизу: «Зараз», «Сьогодні», «Завтра».\n"
    "Відповідаю із затримкою до кількох хвилин.\n"
    "Нагадування приходять автоматично тим, чий chat id додано власником бота.\n"
    "Ваш chat id: {cid}"
).format


async def handle_updates(api: Api, client: httpx.AsyncClient, data) -> None:
    r = await client.get(
        f"{API}/getUpdates",
        params={"offset": bot.STATE.get("offset", 0), "timeout": 0, "allowed_updates": json.dumps(["message"])},
    )
    r.raise_for_status()
    for upd in r.json().get("result", []):
        bot.STATE["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        cid = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip().lower()
        if cid is None or not text:
            continue
        today = bot.datetime.now(bot.TZ).date()
        if text.startswith("/id"):
            answer = f"Ваш chat id: {cid}"
        elif text.startswith(("/start", "/help")):
            answer = HELP(group=bot.GROUP.replace("GPV", "черги "), cid=cid)
        elif not data:
            answer = "Не вдалося отримати дані з GitHub. Спробуйте пізніше."
        elif "зараз" in text or text.startswith("/now"):
            answer = bot.now_text(data)
        elif "сьогодні" in text or text.startswith("/today"):
            answer = bot.day_text(data, today, "Сьогодні")
        elif "завтра" in text or text.startswith("/tomorrow"):
            answer = bot.day_text(data, today + bot.timedelta(days=1), "Завтра")
        else:
            answer = "Натисніть кнопку внизу: «Зараз», «Сьогодні» або «Завтра»."
        try:
            await api.send_message(cid, answer, keyboard=True)
        except Exception as e:  # noqa: BLE001
            bot.log.warning("Не вдалося відповісти %s: %s", cid, e)


async def main() -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        api = Api(client)
        data = await bot.load_data()
        await handle_updates(api, client, data)
        if data:
            await bot.tick(types.SimpleNamespace(bot=api))
    bot.save_state(bot.STATE)  # зберігаємо offset та стан нагадувань


if __name__ == "__main__":
    asyncio.run(main())
