"""Telegram promo bot + tracking redirect server."""

import asyncio
import logging
import os
import sys

from aiohttp import web
from aiogram.exceptions import TelegramConflictError

import db
from bot import get_dispatcher, make_bot
from config import (
    ADMIN_IDS,
    BOT_TOKEN,
    TRACK_VISITORS_CLEANUP_INTERVAL_HOURS,
    TRACK_VISITORS_RETENTION_DAYS,
    WEB_HOST,
    WEB_PORT,
)
from web import create_app

# Render deploy: yangi instance eski hali getUpdates qilayotganda Conflict chiqadi.
# Eski jarayon SIGTERM olishi uchun polling oldidan qisqa kutish.
POLLING_START_DELAY_SEC = float(os.getenv("POLLING_START_DELAY_SEC", "5"))


async def _visitors_cleanup_loop() -> None:
    """Har N soatda retention dan eski track_visitors qatorlarini tozalaydi."""
    days = TRACK_VISITORS_RETENTION_DAYS
    if days < 1:
        logging.info("track_visitors cleanup o'chirilgan (RETENTION_DAYS=%s)", days)
        return
    interval = max(1.0, TRACK_VISITORS_CLEANUP_INTERVAL_HOURS) * 3600
    while True:
        try:
            deleted = await db.cleanup_old_visitors(days)
            if deleted:
                logging.info(
                    "track_visitors cleanup: %s ta qator o'chirildi (>%s kun)",
                    deleted,
                    days,
                )
            else:
                logging.debug("track_visitors cleanup: o'chirishga narsa yo'q")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("track_visitors cleanup xatosi")
        await asyncio.sleep(interval)


async def _start_polling_with_conflict_guard(dp, bot) -> None:
    """Bitta instance polling; Conflict bo'lsa qisqa kutib qayta urinadi."""
    await bot.delete_webhook(drop_pending_updates=True)
    if POLLING_START_DELAY_SEC > 0:
        logging.info(
            "Polling oldidan %.0fs kutish (boshqa instance tugashi uchun)...",
            POLLING_START_DELAY_SEC,
        )
        await asyncio.sleep(POLLING_START_DELAY_SEC)
        await bot.delete_webhook(drop_pending_updates=True)

    logging.info("Polling boshlandi...")
    try:
        await dp.start_polling(bot, handle_signals=True, close_bot_session=False)
    except TelegramConflictError:
        logging.error(
            "Telegram Conflict: boshqa joyda ham shu BOT_TOKEN bilan polling bor "
            "(lokal + Render, yoki eski deploy). 15s kutib qayta uriniladi..."
        )
        await asyncio.sleep(15)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, handle_signals=True, close_bot_session=False)


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
            "ADMIN_IDS bo'sh — super-admin yo'q. "
            "Telegram ID ni .env ga qo'shing (faqat env orqali)."
        )

    # ── Avval web port (Render /health), keyin DB, so'ng bot ──
    # /r/ DB tayyor bo'lguncha 503 qaytaradi (web.redirect_handler).
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEB_HOST, WEB_PORT)
    await site.start()
    logging.info("Kuzatuv serveri: http://%s:%s/r/<token>", WEB_HOST, WEB_PORT)

    cleanup_task: asyncio.Task | None = None
    bot = None
    try:
        try:
            await db.init_db()
            logging.info("DB tayyor.")
        except Exception:
            logging.exception("DB ulanishi muvaffaqiyatsiz.")
            raise

        cleanup_task = asyncio.create_task(
            _visitors_cleanup_loop(), name="track_visitors_cleanup"
        )

        bot = make_bot()
        dp = get_dispatcher()
        await _start_polling_with_conflict_guard(dp, bot)
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:
                logging.exception("bot.session.close() xatosi")
        try:
            await site.stop()
        except Exception:
            logging.exception("site.stop() xatosi")
        try:
            await runner.cleanup()
        except Exception:
            logging.exception("runner.cleanup() xatosi")
        try:
            await db.close_pool()
        except Exception:
            logging.exception("db.close_pool() xatosi")
        logging.info("Kuzatuv serveri va bot sessiyasi yopildi.")


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass
    asyncio.run(main())
