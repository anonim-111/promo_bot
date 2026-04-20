"""Telegram promo bot + tracking redirect server."""

import asyncio
import logging
import os
import sys

from aiohttp import web

import db
from bot import get_dispatcher, make_bot
from config import ADMIN_IDS, BOT_TOKEN, WEB_HOST, WEB_PORT
from web import create_app

# Render da bir nechta pod bo'lsa — faqat bittasi polling qilsin
_POLLING_LOCK_ENV = "RENDER_INSTANCE_ID"


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
            "ADMIN_IDS bo'sh — hech kim botni boshqara olmaydi. "
            "Telegram ID ni .env ga qo'shing."
        )

    await db.init_db()

    # ── Web server (barcha podlarda ishga tushadi) ──
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    logging.info("Kuzatuv serveri: http://%s:%s/r/<token>", WEB_HOST, WEB_PORT)

    # ── Polling faqat bitta podda ishlashi ──
    instance_id = os.getenv(_POLLING_LOCK_ENV, "")
    if instance_id:
        # Render har bir podga RENDER_INSTANCE_ID beradi
        # Faqat birinchi pod (0) polling qilsin
        if not instance_id.endswith("0"):
            logging.info(
                "Bu pod (%s) polling qilmaydi — faqat web server ishlaydi.",
                instance_id,
            )
            # Chiqmasdan web serverni ushlab turadi
            try:
                await asyncio.Event().wait()
            finally:
                await site.stop()
                await runner.cleanup()
            return

    bot = make_bot()
    dp = get_dispatcher()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Polling boshlandi...")
        await dp.start_polling(bot, handle_signals=True)
    finally:
        try:
            await site.stop()
        except Exception:
            logging.exception("site.stop() xatosi")
        try:
            await runner.cleanup()
        except Exception:
            logging.exception("runner.cleanup() xatosi")
        logging.info("Kuzatuv serveri va bot sessiyasi yopildi.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())