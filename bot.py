import asyncio
import sqlite3
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import db
from config import BASE_URL, BOT_TOKEN, TELEGRAM_HTTP_TIMEOUT, is_admin
from link_logo import download_and_save_link_logo
from qr_image import render_tracking_qr_png

router = Router()

# FSM ichida /buyruq va menyuga chiqish uchun
MAIN_MENU_TEXTS = frozenset(
    {
        "➕ Link qo'shish",
        "🎟 Promo kod qo'shish",
        "📱 QR yaratish",
        "📊 Statistika",
        "📋 Linklar",
        "🎟 Promolar",
    }
)

_SAFE_CHUNK = 3800


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Link qo'shish")],
            [KeyboardButton(text="🎟 Promo kod qo'shish")],
            [KeyboardButton(text="📱 QR yaratish"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📋 Linklar"), KeyboardButton(text="🎟 Promolar")],
        ],
        resize_keyboard=True,
    )


class AddLinkStates(StatesGroup):
    waiting_url = State()
    optional_logo = State()


class AddPromoStates(StatesGroup):
    waiting_code = State()


class LogoForLinkStates(StatesGroup):
    waiting_file = State()


class EditLinkStates(StatesGroup):
    waiting_new_url = State()
    waiting_title = State()


def _is_valid_http_url(url: str) -> bool:
    u = urlparse(url.strip())
    return u.scheme in ("http", "https") and bool(u.netloc)


def _h(text: str) -> str:
    """Telegram HTML matn uchun xavfsiz qochirish."""
    return html_escape(text, quote=False)


BTN_MENU = InlineKeyboardButton(text="◀️ Menyuga", callback_data="hm:main")


def _row_menu() -> list[InlineKeyboardButton]:
    return [BTN_MENU]


def _kb_menu_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_row_menu()])


def _kb_after_qr_batch(link_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Promo tanlash",
                    callback_data=f"qr:bp:{link_id}",
                )
            ],
            _row_menu(),
        ]
    )


def _kb_bulk_qr_style_pick(link_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎨 Rangli",
                    callback_data=f"qall:{link_id}:r",
                ),
                InlineKeyboardButton(
                    text="⬛ Oq-qora",
                    callback_data=f"qall:{link_id}:s",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Promo tanlash",
                    callback_data=f"qr:bp:{link_id}",
                )
            ],
            _row_menu(),
        ]
    )


async def _send_bulk_qr_style_prompt(message: Message, link_id: int) -> None:
    await message.answer(
        "📦 <b>Barcha promo</b> — QR uslubini tanlang:",
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_bulk_qr_style_pick(link_id),
    )


def _kb_link_detail(link_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ O'zgartirish", callback_data=f"le:{link_id}"
                ),
                InlineKeyboardButton(
                    text="🖼 Logotipni almashtirish",
                    callback_data=f"lg:{link_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🗑 O'chirish", callback_data=f"ld:{link_id}"
                ),
            ],
            [
                InlineKeyboardButton(text="◀️ Ro'yxatga", callback_data="lb:list"),
                BTN_MENU,
            ],
        ]
    )


async def _send_link_detail(message: Message, link_id: int) -> None:
    """Logotip surati (bo'lsa) + matn + tahrir / logo / o'chirish tugmalari."""
    row = await db.get_link(link_id)
    if not row:
        await message.answer("Link topilmadi.")
        return
    u = row["url"]
    t = (row.get("title") or "").strip()
    caption = f"<b>Link #{link_id}</b>\n<b>URL</b>:\n<code>{_h(u)}</code>"
    if t:
        caption += f"\n<b>Sarlavha</b>: {_h(t)}"

    kb = _kb_link_detail(link_id)

    logo_path: Path | None = None
    lp = (row.get("logo_path") or "").strip()
    if lp:
        p = Path(lp)
        if p.is_file():
            logo_path = p
    if logo_path is None:
        disk = db.disk_path_for_link_logo(link_id)
        if disk.is_file():
            logo_path = disk

    if logo_path is not None:
        data = logo_path.read_bytes()
        photo = BufferedInputFile(data, filename="link_logo.png")
        cap = caption
        if len(cap) > 900:
            cap = cap[:897] + "…"
        await message.answer_photo(
            photo=photo,
            caption=cap,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    else:
        await message.answer(
            caption + "\n\n<i>Logotip biriktirilmagan.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )


async def _show_links_list_message(message: Message, intro: str) -> None:
    rows = await db.list_links()
    if not rows:
        await message.answer("Hozircha linklar yo'q.", reply_markup=_kb_menu_only())
        return
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{r['id']}: {(r['url'])[:48]}…",
                callback_data=f"ll:{r['id']}",
            )
        ]
        for r in rows[:30]
    ]
    buttons.append(_row_menu())
    await message.answer(
        intro,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def _link_logo_offer_links(message: Message) -> None:
    rows = await db.list_links()
    if not rows:
        await message.answer(
            "Avval «➕ Link qo'shish» bilan link qo'shing.",
            reply_markup=_kb_menu_only(),
        )
        return
    buttons = [
        [
            InlineKeyboardButton(
                text=(r.get("title") or r["url"])[:55],
                callback_data=f"lg:{r['id']}",
            )
        ]
        for r in rows[:30]
    ]
    buttons.append(_row_menu())
    await message.answer(
        "Qaysi link uchun QR markazidagi logotipni o'rnatmoqchisiz — tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


def _split_html_lines(lines: list[str]) -> list[str]:
    """Telegram 4096 cheklovidan oshmaslik uchun."""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        add = len(line) + (1 if buf else 0)
        if buf and size + add > _SAFE_CHUNK:
            chunks.append("\n".join(buf))
            buf = [line]
            size = len(line)
        else:
            buf.append(line)
            size += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks or [""]


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Bu bot faqat administratorlar uchun.")
        return
    await message.answer("Salom!", reply_markup=main_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    await message.answer(
        "1) Avval <b>Link</b> va <b>Promo kod</b> qo'shing.\n"
        "2) <b>QR yaratish</b> — link, promo, uslub; «📦 Barcha promo uchun QR» da "
        "avval rangli yoki oq-qora tanlanadi.\n"
        "3) <b>Statistika</b> — avval linkni tanlang, keyin promo kodlar va yuklanishlar.\n"
        "4) Har bir link uchun QR markazidagi logotip — link qo'shganda yoki "
        "<b>📋 Linklar</b> → link kartochkasidagi <b>Logotipni almashtirish</b>.\n"
        "5) <b>📋 Linklar</b> — tahrir, logotip va o'chirish ham shu yerda.\n\n"
        f"Tracking URL ko'rinishi: <code>{_h(BASE_URL)}/r/token</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_menu_only(),
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=main_kb())


def _deny(message: Message) -> bool:
    if message.from_user and is_admin(message.from_user.id):
        return False
    return True


@router.message(F.text == "➕ Link qo'shish")
async def add_link_prompt(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    await state.set_state(AddLinkStates.waiting_url)
    await message.answer(
        "To'liq URL yuboring (https://...).\nBekor qilish: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AddLinkStates.waiting_url, F.text.in_(MAIN_MENU_TEXTS))
@router.message(AddLinkStates.optional_logo, F.text.in_(MAIN_MENU_TEXTS))
@router.message(AddPromoStates.waiting_code, F.text.in_(MAIN_MENU_TEXTS))
@router.message(LogoForLinkStates.waiting_file, F.text.in_(MAIN_MENU_TEXTS))
@router.message(EditLinkStates.waiting_new_url, F.text.in_(MAIN_MENU_TEXTS))
@router.message(EditLinkStates.waiting_title, F.text.in_(MAIN_MENU_TEXTS))
async def fsm_to_main_menu(message: Message, state: FSMContext) -> None:
    """FSM ichida pastki menyu tugmalari — holatni tozalaydi va buyruqni bajaradi."""
    if _deny(message):
        return
    await state.clear()
    text = message.text or ""
    if text == "📊 Statistika":
        await stats_cmd(message)
    elif text == "📋 Linklar":
        await list_links_cmd(message)
    elif text == "🎟 Promolar":
        await list_promos_cmd(message)
    elif text == "📱 QR yaratish":
        await qr_pick_link(message)
    elif text == "➕ Link qo'shish":
        await add_link_prompt(message, state)
    elif text == "🎟 Promo kod qo'shish":
        await add_promo_prompt(message, state)


@router.callback_query(F.data == "hm:main")
async def callback_home_menu(callback: CallbackQuery) -> None:
    """Inline menyudan pastki klaviaturaga qaytish."""
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer("Pastki menyu:", reply_markup=main_kb())
    await callback.answer()


@router.callback_query(F.data == "lb:list")
async def link_list_back(callback: CallbackQuery) -> None:
    """Link kartochkasidan ro'yxatga qaytish."""
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _show_links_list_message(
            callback.message,
            "📋 <b>Linklar</b>\n\nKerakli linkni tanlang:",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("ll:"))
async def link_detail_open(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        link_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id):
        await callback.answer("Link topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_link_detail(callback.message, link_id)
    await callback.answer()


@router.callback_query(F.data.startswith("le:"))
async def link_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        link_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id):
        await callback.answer("Link topilmadi", show_alert=True)
        return
    await state.set_state(EditLinkStates.waiting_new_url)
    await state.update_data(edit_link_id=link_id)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "Yangi <b>to'liq URL</b> yuboring (<code>https://...</code>).\nBekor: /cancel",
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


@router.message(EditLinkStates.waiting_new_url, F.text & ~F.text.startswith("/"))
async def link_edit_save_url(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    data = await state.get_data()
    lid = data.get("edit_link_id")
    if not isinstance(lid, int):
        await state.clear()
        return
    url = (message.text or "").strip()
    if not _is_valid_http_url(url):
        await message.answer("Noto'g'ri URL. https:// bilan qayta yuboring.")
        return
    await db.update_link_fields(lid, url=url)
    await state.set_state(EditLinkStates.waiting_title)
    await message.answer(
        "URL yangilandi.\n\n"
        "<b>Sarlavha</b> (ixtiyoriy, ro'yxatda qisqa ko'rinish uchun) yuboring yoki /skip",
        parse_mode=ParseMode.HTML,
    )


@router.message(EditLinkStates.waiting_title, Command("skip"))
async def link_edit_skip_title(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    await state.clear()
    await message.answer("Tahrir yakunlandi.", reply_markup=main_kb())


@router.message(EditLinkStates.waiting_title, F.text)
async def link_edit_save_title(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    data = await state.get_data()
    lid = data.get("edit_link_id")
    if not isinstance(lid, int):
        await state.clear()
        return
    title = (message.text or "").strip()
    await db.update_link_fields(lid, title=title if title else None)
    await state.clear()
    await message.answer("Sarlavha saqlandi.", reply_markup=main_kb())


@router.callback_query(F.data.startswith("ld:"))
async def link_delete_ask(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        link_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id):
        await callback.answer("Link topilmadi", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Ha, o'chirish",
                    callback_data=f"ldc:{link_id}",
                )
            ],
            [InlineKeyboardButton(text="❌ Yo'q", callback_data=f"ll:{link_id}")],
            _row_menu(),
        ]
    )
    if callback.message:
        await callback.message.answer(
            f"<b>Link #{link_id}</b> va unga bog'langan barcha tracking/QR yozuvlari "
            f"o'chiriladi. Davom etasizmi?",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("ldc:"))
async def link_delete_do(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        link_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    ok = await db.delete_link(link_id)
    if not ok:
        await callback.answer("Link topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            f"✅ Link <code>{link_id}</code> o'chirildi.",
            parse_mode=ParseMode.HTML,
        )
    await callback.answer("O'chirildi")


@router.message(AddLinkStates.waiting_url, F.text & ~F.text.startswith("/"))
async def add_link_save(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    url = (message.text or "").strip()
    if not _is_valid_http_url(url):
        await message.answer("Noto'g'ri URL. https:// bilan qayta yuboring.")
        return
    lid = await db.add_link(url)
    await state.update_data(pending_link_id=lid)
    await state.set_state(AddLinkStates.optional_logo)
    await message.answer(
        f"✅ Link saqlandi (id: <code>{lid}</code>).\n\n"
        "QR markazidagi <b>logotip</b> uchun surat yoki PNG/JPEG <b>dokument</b> yuboring.\n"
        "O'tkazib yuborish: /skip",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AddLinkStates.optional_logo, Command("skip"))
async def add_link_skip_logo(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    await state.clear()
    await message.answer("Logotipsiz saqlandi.", reply_markup=main_kb())


@router.message(AddLinkStates.optional_logo, F.text)
async def add_link_logo_hint(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    await message.answer(
        "Logotipni <b>surat</b> yoki <b>dokument</b> (PNG/JPEG) sifatida yuboring, yoki /skip",
        parse_mode=ParseMode.HTML,
    )


@router.message(AddLinkStates.optional_logo, F.document | F.photo)
async def add_link_save_logo(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    data = await state.get_data()
    lid = data.get("pending_link_id")
    if not isinstance(lid, int):
        await state.clear()
        return
    try:
        await download_and_save_link_logo(message, lid)
    except ValueError as e:
        await message.answer(_h(str(e)))
        return
    await state.clear()
    await _send_link_detail(message, lid)
    await message.answer("✅", reply_markup=main_kb())


@router.message(F.text == "🎟 Promo kod qo'shish")
async def add_promo_prompt(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    await state.set_state(AddPromoStates.waiting_code)
    await message.answer(
        "Promo kod matnini yuboring (masalan: SUMMER2026).\nBekor qilish: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AddPromoStates.waiting_code, F.text & ~F.text.startswith("/"))
async def add_promo_save(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    code = (message.text or "").strip()
    if len(code) < 2:
        await message.answer("Kod juda qisqa.")
        return
    try:
        pid = await db.add_promo(code)
    except sqlite3.IntegrityError:
        await message.answer("Bu promo kod allaqachon mavjud.")
        return
    await state.clear()
    await message.answer(f"✅ Promo saqlandi (id: {pid}).", reply_markup=main_kb())


@router.message(F.text == "📋 Linklar")
async def list_links_cmd(message: Message) -> None:
    if _deny(message):
        return
    await _show_links_list_message(
        message,
        "📋 <b>Linklar</b>\n\nTanlang — keyin logotip va tahrir tugmalari chiqadi:",
    )


@router.message(F.text == "🎟 Promolar")
async def list_promos_cmd(message: Message) -> None:
    if _deny(message):
        return
    rows = await db.list_promos()
    if not rows:
        await message.answer(
            "Hozircha promo kodlar yo'q.",
            reply_markup=_kb_menu_only(),
        )
        return
    lines = [
        f"• <code>{r['id']}</code> — <code>{_h(r['code'])}</code>" for r in rows
    ]
    parts = _split_html_lines(lines)
    for i, part in enumerate(parts):
        await message.answer(
            part,
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_menu_only() if i == len(parts) - 1 else None,
        )


def _kb_stats_detail() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Linklar ro'yxati",
                    callback_data="stat:list",
                )
            ],
            _row_menu(),
        ]
    )


async def _show_stats_link_list(message: Message) -> None:
    rows = await db.list_links()
    if not rows:
        await message.answer(
            "Hozircha linklar yo'q.",
            reply_markup=_kb_menu_only(),
        )
        return
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{r['id']}: {(r.get('title') or r['url'])[:48]}…",
                callback_data=f"stat:lnk:{r['id']}",
            )
        ]
        for r in rows[:30]
    ]
    buttons.append(_row_menu())
    await message.answer(
        "📊 <b>Statistika</b>\n\n"
        "Kerakli <b>linkni</b> tanlang — promo kodlar va yuklanishlar chiqadi:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def _send_stats_for_link_detail(message: Message, link_id: int) -> None:
    row = await db.get_link(link_id)
    if not row:
        await message.answer("Link topilmadi.", reply_markup=_kb_menu_only())
        return
    stats = await db.stats_for_link(link_id)
    u = row["url"]
    t = (row.get("title") or "").strip()
    head = (
        "📊 <b>Statistika</b>\n"
        f"<b>Link #{link_id}</b>" + (f" — {_h(t)}" if t else "") + "\n"
        f"<code>{_h(u)}</code>\n\n"
        "<b>Promo va yuklanishlar</b>\n"
    )
    if not stats:
        promo_lines = ["<i>Hozircha promo kodlar yo'q.</i>"]
    else:
        promo_lines = [
            f"• <code>{_h(s['promo_code'])}</code> — "
            f"<b>{int(s['clicks'] or 0)}</b> yuklanish"
            for s in stats
        ]
    parts = _split_html_lines(promo_lines)
    kb = _kb_stats_detail()
    messages_out: list[str] = []
    for i, p in enumerate(parts):
        prefix = head if i == 0 else "📊 <i>Davomi</i>\n\n"
        messages_out.append(prefix + p)
    for i, text in enumerate(messages_out):
        await message.answer(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb if i == len(messages_out) - 1 else None,
        )


@router.callback_query(F.data == "stat:list")
async def stats_back_to_link_list(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _show_stats_link_list(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("stat:lnk:"))
async def stats_open_link(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        link_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id):
        await callback.answer("Link topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_stats_for_link_detail(callback.message, link_id)
    await callback.answer()


@router.message(F.text == "📊 Statistika")
async def stats_cmd(message: Message) -> None:
    if _deny(message):
        return
    links = await db.list_links()
    if not links:
        await message.answer(
            "Statistika uchun avval <b>link</b> qo'shing.",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_menu_only(),
        )
        return
    promos = await db.list_promos()
    if not promos:
        await message.answer(
            "Statistika uchun avval <b>promo kod</b> qo'shing.",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_menu_only(),
        )
        return
    await _show_stats_link_list(message)


async def _send_qr_link_pick(target: Message) -> bool:
    """QR oqimi: link tanlash. False bo'lsa shartlar bajarilmagan."""
    links = await db.list_links()
    if not links:
        await target.answer(
            "Avval «Link qo'shish» orqali link qo'shing.",
            reply_markup=_kb_menu_only(),
        )
        return False
    promos = await db.list_promos()
    if not promos:
        await target.answer(
            "Avval «Promo kod qo'shish» orqali promo qo'shing.",
            reply_markup=_kb_menu_only(),
        )
        return False
    buttons = [
        [
            InlineKeyboardButton(
                text=(l.get("title") or l["url"])[:60],
                callback_data=f"ql:{l['id']}",
            )
        ]
        for l in links[:30]
    ]
    buttons.append(_row_menu())
    await target.answer(
        "Tracking uchun linkni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    return True


async def _send_qr_promo_pick(target: Message, link_id: int) -> None:
    promos = await db.list_promos()
    if not promos:
        await target.answer(
            "Promo kodlar yo'q. Avval «🎟 Promo kod qo'shish» bilan qo'shing.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="◀️ Link tanlash",
                            callback_data="qr:bl",
                        )
                    ],
                    _row_menu(),
                ]
            ),
        )
        return
    buttons = [
        [
            InlineKeyboardButton(
                text="📦 Barcha promo uchun QR",
                callback_data=f"qbulk:{link_id}",
            ),
        ],
        *[
            [
                InlineKeyboardButton(
                    text=p["code"][:60], callback_data=f"qp:{link_id}:{p['id']}"
                )
            ]
            for p in promos[:30]
        ],
    ]
    buttons.append(
        [
            InlineKeyboardButton(text="◀️ Link tanlash", callback_data="qr:bl"),
            BTN_MENU,
        ]
    )
    await target.answer(
        "Promo kodni tanlang yoki barchasi uchun birdaniga QR oling:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def _deliver_bulk_promo_qrs(
    message: Message, link_id: int, style: str
) -> None:
    promos = await db.list_promos()
    if not promos:
        await message.answer(
            "Promo kodlar yo'q.",
            reply_markup=_kb_menu_only(),
        )
        return
    link_row = await db.get_link(link_id)
    if not link_row:
        await message.answer("Link topilmadi.", reply_markup=_kb_menu_only())
        return
    logo_override: str | None = None
    if link_row:
        lp = (link_row.get("logo_path") or "").strip()
        if lp and Path(lp).is_file():
            logo_override = lp
    uslub = "Oddiy (oq-qora)" if style == "simple" else "Rangli"
    total = len(promos)
    await message.answer(
        f"📦 <b>{total}</b> ta promo uchun QR yuborilmoqda "
        f"(<i>{uslub}</i>)…",
        parse_mode=ParseMode.HTML,
    )
    for i, p in enumerate(promos):
        promo_id = p["id"]
        code = p["code"]
        token = await db.create_track_entry(link_id, promo_id)
        tracking_url = f"{BASE_URL}/r/{token}"
        png_bytes = render_tracking_qr_png(
            tracking_url, logo_path=logo_override, style=style
        )
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in code)[
            :40
        ]
        file = BufferedInputFile(png_bytes, filename=f"qr_{safe_name or promo_id}.png")
        last = i == total - 1
        await message.answer_photo(
            photo=file,
            caption=(
                f"<b>Promo</b>: <code>{_h(code)}</code>\n"
                f"<b>Uslub</b>: {uslub}\n"
                f"<b>Havola</b>:\n<code>{_h(tracking_url)}</code>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_after_qr_batch(link_id) if last else None,
        )
        if not last:
            await asyncio.sleep(0.35)


@router.message(F.text == "📱 QR yaratish")
async def qr_pick_link(message: Message) -> None:
    if _deny(message):
        return
    await _send_qr_link_pick(message)


@router.callback_query(F.data == "qr:bl")
async def qr_back_to_links(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_qr_link_pick(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("qr:bp:"))
async def qr_back_to_promos(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        link_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id):
        await callback.answer("Link topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_qr_promo_pick(callback.message, link_id)
    await callback.answer()


@router.callback_query(F.data.startswith("qbulk:"))
async def qr_bulk_open_style_menu(callback: CallbackQuery) -> None:
    """Barcha promo QR — avval rangli / oq-qora tanlash."""
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        link_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id):
        await callback.answer("Link topilmadi", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _send_bulk_qr_style_prompt(callback.message, link_id)


@router.callback_query(F.data.startswith("qall:"))
async def qr_all_promos_bulk(callback: CallbackQuery) -> None:
    """Tanlangan link uchun bazadagi barcha promolar bo'yicha QR yuborish."""
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) < 2 or parts[0] != "qall":
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id):
        await callback.answer("Link topilmadi", show_alert=True)
        return
    if len(parts) < 3 or parts[2] not in ("s", "r"):
        await callback.answer()
        if callback.message:
            await _send_bulk_qr_style_prompt(callback.message, link_id)
        return
    mode = parts[2]
    style = "simple" if mode == "s" else "styled"
    await callback.answer("QRlar yuborilmoqda…")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _deliver_bulk_promo_qrs(callback.message, link_id, style)


@router.callback_query(F.data.startswith("lg:"))
async def link_logo_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        link_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id):
        await callback.answer("Link topilmadi", show_alert=True)
        return
    await state.set_state(LogoForLinkStates.waiting_file)
    await state.update_data(logo_link_id=link_id)
    if callback.message:
        await callback.message.answer(
            f"Link id <code>{link_id}</code>: logotip yuboring (surat yoki PNG dokument).\n"
            "Bekor: /cancel",
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


@router.message(LogoForLinkStates.waiting_file, F.document | F.photo)
async def link_logo_save_for_existing(message: Message, state: FSMContext) -> None:
    if _deny(message):
        return
    data = await state.get_data()
    lid = data.get("logo_link_id")
    if not isinstance(lid, int):
        await state.clear()
        return
    try:
        await download_and_save_link_logo(message, lid)
    except ValueError as e:
        await message.answer(_h(str(e)))
        return
    await state.clear()
    await _send_link_detail(message, lid)
    await message.answer("✅", reply_markup=main_kb())


@router.callback_query(F.data.startswith("ql:"))
async def qr_pick_promo(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        link_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_qr_promo_pick(callback.message, link_id)
    await callback.answer()


@router.callback_query(F.data.startswith("qp:"))
async def qr_choose_style(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        promo_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return

    style_row = [
        InlineKeyboardButton(
            text="⬛ Oddiy (oq-qora)",
            callback_data=f"qst:{link_id}:{promo_id}:s",
        ),
        InlineKeyboardButton(
            text="🎨 Rangli",
            callback_data=f"qst:{link_id}:{promo_id}:r",
        ),
    ]
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            "QR kod turini tanlang:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    style_row,
                    [
                        InlineKeyboardButton(
                            text="◀️ Promo tanlash",
                            callback_data=f"qr:bp:{link_id}",
                        ),
                    ],
                    _row_menu(),
                ]
            ),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("qst:"))
async def qr_build(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        promo_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    mode = parts[3]
    if mode not in ("s", "r"):
        await callback.answer("Xato", show_alert=True)
        return
    style = "simple" if mode == "s" else "styled"

    token = await db.create_track_entry(link_id, promo_id)
    tracking_url = f"{BASE_URL}/r/{token}"
    promo_label = await db.get_promo_code_by_id(promo_id) or "?"

    link_row = await db.get_link(link_id)
    logo_override: str | None = None
    if link_row:
        lp = (link_row.get("logo_path") or "").strip()
        if lp and Path(lp).is_file():
            logo_override = lp

    png_bytes = render_tracking_qr_png(
        tracking_url, logo_path=logo_override, style=style
    )
    file = BufferedInputFile(png_bytes, filename="promo_qr.png")

    uslub = "Oddiy (oq-qora)" if style == "simple" else "Rangli"

    if not callback.message:
        await callback.answer("Xabar topilmadi", show_alert=True)
        return
    await callback.message.answer_photo(
        photo=file,
        caption=(
            f"<b>Promo kod</b>: <code>{_h(promo_label)}</code>\n"
            f"<b>Uslub</b>: {uslub}\n"
            f"<b>Havola</b>:\n<code>{_h(tracking_url)}</code>"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_menu_only(),
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("Tayyor")


def get_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


def make_bot() -> Bot:
    """Telegram API uchun uzoq timeout — tarmoq uzilishi / Windows ClientOSError kamayishi mumkin."""
    session = AiohttpSession(timeout=TELEGRAM_HTTP_TIMEOUT)
    return Bot(BOT_TOKEN, session=session)


async def run_bot_polling() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN .env faylida yo'q")
    bot = make_bot()
    dp = get_dispatcher()
    await dp.start_polling(bot)
