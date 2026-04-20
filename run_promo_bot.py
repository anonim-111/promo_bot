"""Telegram promo bot + tracking redirect server."""

import asyncio
import logging
import sys

from aiohttp import web

import db
from bot import get_dispatcher, make_bot
from config import ADMIN_IDS, BOT_TOKEN, WEB_HOST, WEB_PORT
from web import create_app


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN .env faylida yo'q.")
        sys.exit(1)
    if not ADMIN_IDS:
        logging.warning(
            "ADMIN_IDS bo'sh — hech kim botni boshqara olmaydi. Telegram ID ni .env ga qo'shing."
        )

    await db.init_db()
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    logging.info("Kuzatuv serveri: http://%s:%s/r/<token>", WEB_HOST, WEB_PORT)

    bot = make_bot()
    dp = get_dispatcher()
    await dp.start_polling(bot)


if __name__ == "__main__":
    # Ba'zi Windows muhitlarida aiohttp tarmoq xatolari kamayishi mumkin
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
