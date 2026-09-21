import asyncio
import logging
from datetime import datetime
from html import escape as html_escape
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
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
from openpyxl import Workbook
from openpyxl.styles import Font

import access
import db
from access import (
    can_access_category,
    can_access_group,
    can_manage,
    can_manage_users,
    can_view_stats,
    category_scope,
    get_role,
)
from config import BASE_URL, BOT_TOKEN, TELEGRAM_HTTP_TIMEOUT, is_super_admin
from db import DuplicateError
from link_logo import download_and_save_link_logo
from qr_image import excel_inline_qr_png, render_tracking_qr_png

router = Router()

# Pastki menyu tugmalari (matn o'zgarsa — MAIN_MENU_TEXTS va handlerlar ham)
BTN_ADD_LINK = "➕ Yangi link"
BTN_ADD_PROMO = "🎟 Yangi promo"
BTN_QR = "📱 QR kod yaratish"
BTN_LINKS = "📋 Linklarni ko'rish"
BTN_PROMOS = "🎟 Promolarni ko'rish"
BTN_STATS = "📊 Statistikani ko'rish"
BTN_ADD_CATEGORY = "🏛 Yangi kategoriya"
BTN_ADD_GROUP = "📁 Yangi guruh"
BTN_CATEGORIES = "🏛 Kategoriyani boshqarish"
BTN_USERS = "👥 Foydalanuvchilar"

# FSM ichida /buyruq va menyuga chiqish uchun
MAIN_MENU_TEXTS = frozenset(
    {
        BTN_ADD_LINK,
        BTN_ADD_PROMO,
        BTN_QR,
        BTN_LINKS,
        BTN_PROMOS,
        BTN_STATS,
        BTN_ADD_CATEGORY,
        BTN_ADD_GROUP,
        BTN_CATEGORIES,
        BTN_USERS,
    }
)

_SAFE_CHUNK = 3800
# Telegram flood/timeout oldini olish: bundan ko'p rasm o'rniga Excel.
BULK_QR_MAX_PHOTOS = 40

# Ro'yxatlarni sahifalash uchun umumiy sahifa hajmi.
LIST_PAGE_SIZE = 25


def _page_slice(items: list, page: int) -> tuple[list, int, int, int]:
    """Ro'yxatni sahifalarga bo'ladi.

    Qaytaradi: (shu sahifadagi elementlar, tozalangan page, jami sahifa, jami son).
    page 0..total_pages-1 oralig'iga cheklanadi.
    """
    total = len(items)
    total_pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * LIST_PAGE_SIZE
    chunk = items[start : start + LIST_PAGE_SIZE]
    return chunk, page, total_pages, total


def _pagination_row(
    prev_cd: str | None, next_cd: str | None, page: int, total_pages: int
) -> list[InlineKeyboardButton]:
    """Sahifalash qatori: ◀️ Oldingi / N/M / Keyingi ▶️. Bitta sahifa bo'lsa — bo'sh."""
    if total_pages <= 1:
        return []
    row: list[InlineKeyboardButton] = []
    if prev_cd:
        row.append(InlineKeyboardButton(text="◀️ Oldingi", callback_data=prev_cd))
    row.append(
        InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop")
    )
    if next_cd:
        row.append(InlineKeyboardButton(text="Keyingi ▶️", callback_data=next_cd))
    return row


def _with_page_note(text: str, page: int, total_pages: int) -> str:
    if total_pages > 1:
        return f"{text}\n\n<i>Sahifa {page + 1}/{total_pages}</i>"
    return text


async def _send_or_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    *,
    edit: bool,
    parse_mode: str | None = None,
) -> None:
    """Sahifalashda mavjud xabarni tahrirlaydi; muvaffaqiyatsiz bo'lsa yangi yuboradi."""
    if edit:
        try:
            await message.edit_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup
            )
            return
        except TelegramBadRequest:
            pass
    await message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)


@router.callback_query(F.data == "noop")
async def _cb_noop(callback: CallbackQuery) -> None:
    """Sahifa raqami ko'rsatkichi — bosilganda hech nima qilmaydi."""
    await callback.answer()


def main_kb() -> ReplyKeyboardMarkup:
    """Admin pastki menyu — 3 ustun."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_ADD_LINK),
                KeyboardButton(text=BTN_ADD_PROMO),
                KeyboardButton(text=BTN_QR),
            ],
            [
                KeyboardButton(text=BTN_LINKS),
                KeyboardButton(text=BTN_PROMOS),
                KeyboardButton(text=BTN_STATS),
            ],
            [
                KeyboardButton(text=BTN_ADD_CATEGORY),
                KeyboardButton(text=BTN_ADD_GROUP),
                KeyboardButton(text=BTN_CATEGORIES),
            ],
        ],
        resize_keyboard=True,
    )


def viewer_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_STATS)]],
        resize_keyboard=True,
    )


def super_kb() -> ReplyKeyboardMarkup:
    kb = main_kb()
    rows = list(kb.keyboard)
    rows.append([KeyboardButton(text=BTN_USERS)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def reply_kb_for(
    user_id: int,
) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    role = await get_role(user_id)
    if role == access.ROLE_SUPER:
        return super_kb()
    if role == access.ROLE_ADMIN:
        return main_kb()
    if role == access.ROLE_VIEWER:
        return viewer_kb()
    return ReplyKeyboardRemove()


async def _kb(message: Message) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    if message.from_user:
        return await reply_kb_for(message.from_user.id)
    return ReplyKeyboardRemove()


class AddLinkStates(StatesGroup):
    waiting_url = State()
    optional_logo = State()


class AddPromoStates(StatesGroup):
    waiting_group = State()
    waiting_code = State()


class LogoForLinkStates(StatesGroup):
    waiting_file = State()


class EditLinkStates(StatesGroup):
    waiting_new_url = State()
    waiting_title = State()


class EditPromoStates(StatesGroup):
    waiting_code = State()


class AddCategoryStates(StatesGroup):
    waiting_name = State()


class AddGroupStates(StatesGroup):
    waiting_category = State()
    waiting_name = State()


class EditCategoryStates(StatesGroup):
    waiting_name = State()


class EditGroupNameStates(StatesGroup):
    waiting_name = State()


class UserManageStates(StatesGroup):
    waiting_admin_id = State()
    waiting_viewer_id = State()


def _is_valid_http_url(url: str) -> bool:
    u = urlparse(url.strip())
    return u.scheme in ("http", "https") and bool(u.netloc)


def _h(text: str) -> str:
    """Telegram HTML matn uchun xavfsiz qochirish."""
    return html_escape(text, quote=False)


def _h_attr(url: str) -> str:
    """HTML atribut (masalan <a href=\"...\">) uchun."""
    return html_escape(url.strip(), quote=True)


def _caption_tracking_link(tracking_url: str) -> str:
    """Telegramda bosiladigan havola: <code> ichidagi URL bosilmaydi."""
    u = tracking_url.strip()
    return f"<b>Havola</b>:\n<a href=\"{_h_attr(u)}\">{_h(u)}</a>"


BTN_MENU = InlineKeyboardButton(text="◀️ Menyuga", callback_data="hm:main")


def _row_menu() -> list[InlineKeyboardButton]:
    return [BTN_MENU]


def _kb_menu_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_row_menu()])


def _kb_after_qr_batch(
    link_id: int,
    group_id: int,
    *,
    style: str | None = None,
    next_offset: int | None = None,
    remaining: int = 0,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if next_offset is not None and style in ("simple", "styled") and remaining > 0:
        style_flag = "s" if style == "simple" else "r"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"▶️ Keyingisi ({remaining} ta qoldi)",
                    callback_data=(
                        f"qmore:{link_id}:{group_id}:{style_flag}:{next_offset}"
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="◀️ Guruhdagi promolar",
                callback_data=f"qrg:{link_id}:{group_id}",
            )
        ]
    )
    rows.append(_row_menu())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_bulk_qr_style_pick_group(link_id: int, group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎨 Rangli",
                    callback_data=f"qallg:{link_id}:{group_id}:r",
                ),
                InlineKeyboardButton(
                    text="⬛ Oq-qora",
                    callback_data=f"qallg:{link_id}:{group_id}:s",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Guruhdagi promolar",
                    callback_data=f"qrg:{link_id}:{group_id}",
                )
            ],
            _row_menu(),
        ]
    )


async def _send_bulk_qr_style_prompt_group(
    message: Message, link_id: int, group_id: int
) -> None:
    await message.answer(
        "📦 <b>Guruhdagi barcha promo</b> — QR uslubini tanlang:",
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_bulk_qr_style_pick_group(link_id, group_id),
    )


def _build_tracking_links_excel(
    rows: list[tuple[str, str, str, str]],
) -> bytes:
    """(kategoriya, guruh, promo, url) — har kategoriya alohida sheet; parallel QR."""
    from concurrent.futures import ThreadPoolExecutor
    import os

    import xlsxwriter
    from PIL import Image

    if not rows:
        raise ValueError("Excel uchun qatorlar bo'sh")

    urls = [url for *_, url in rows]

    def _png(url: str) -> bytes | None:
        try:
            return excel_inline_qr_png(url)
        except Exception:
            logging.exception("Excel QR xato: %s", url[:80])
            return None

    workers = min(32, max(4, (os.cpu_count() or 4) * 2))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pngs = list(pool.map(_png, urls, chunksize=16))

    # Kategoriya tartibi — rows dagi birinchi uchrashuv tartibida
    by_cat: dict[str, list[tuple[str, str, str, bytes | None]]] = {}
    for (cat, g_name, code, url), png in zip(rows, pngs):
        key = (cat or "").strip() or "Noma'lum"
        by_cat.setdefault(key, []).append((g_name, code, url, png))

    bio = BytesIO()
    # constant_memory rasmlarni qo'llab-quvvatlamaydi — o'chirilgan.
    wb = xlsxwriter.Workbook(
        bio,
        {
            "in_memory": True,
            "strings_to_urls": False,
        },
    )
    header = wb.add_format({"bold": True})
    used_titles: set[str] = set()

    for sheet_i, (cat_name, items) in enumerate(by_cat.items()):
        ws = wb.add_worksheet(_excel_sheet_title(cat_name, used_titles))
        ws.write_row(0, 0, ["Guruh", "Promo kod", "Tracking havola", "QR"], header)
        ws.set_column(0, 0, 34)
        ws.set_column(1, 1, 40)
        # URL to'liq ko'rinsin (Excel birligi ~1 belgi); uzun domen uchun moslashadi
        max_url_len = max((len(url) for _, _, url, _ in items), default=60)
        ws.set_column(2, 2, min(120, max(72, max_url_len + 4)))

        max_qr_px = 0
        for row_idx, (g_name, code, url, png) in enumerate(items, start=1):
            ws.write(row_idx, 0, g_name)
            ws.write(row_idx, 1, code)
            ws.write(row_idx, 2, url)
            if not png:
                ws.set_row(row_idx, 18)
                continue
            with Image.open(BytesIO(png)) as im:
                qr_w, qr_h = im.size
            max_qr_px = max(max_qr_px, qr_w)
            # Excel qator balandligi punktlarda (~96dpi: px * 0.75) + offset
            ws.set_row(row_idx, qr_h * 0.75 + 4)
            ws.insert_image(
                row_idx,
                3,
                f"qr_{sheet_i}_{row_idx}.png",
                {
                    "image_data": BytesIO(png),
                    "object_position": 1,
                    "x_offset": 2,
                    "y_offset": 2,
                },
            )
        # Ustun ~7px/birlik + padding; kamida 12
        col_w = max(12, (max_qr_px + 12) / 7) if max_qr_px else 12
        ws.set_column(3, 3, col_w)
    wb.close()
    return bio.getvalue()


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


async def _show_links_list_message(
    message: Message, intro: str, *, page: int = 0, edit: bool = False
) -> None:
    rows = await db.list_links()
    if not rows:
        await message.answer("Hozircha linklar yo'q.", reply_markup=_kb_menu_only())
        return
    chunk, page, total_pages, _total = _page_slice(rows, page)
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{r['id']}: {(r['url'])[:48]}…",
                callback_data=f"ll:{r['id']}",
            )
        ]
        for r in chunk
    ]
    nav_row = _pagination_row(
        f"navll:{page - 1}" if page > 0 else None,
        f"navll:{page + 1}" if page < total_pages - 1 else None,
        page,
        total_pages,
    )
    if nav_row:
        buttons.append(nav_row)
    buttons.append(_row_menu())
    await _send_or_edit(
        message,
        _with_page_note(intro, page, total_pages),
        InlineKeyboardMarkup(inline_keyboard=buttons),
        edit=edit,
        parse_mode=ParseMode.HTML,
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


async def _send_promo_categories_pick(
    message: Message,
    *,
    intro: str,
    prefix: str,
    include_menu: bool = True,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
    page: int = 0,
    edit: bool = False,
) -> bool:
    """Kategoriya tanlash tugmalari. prefix masalan: apc, pcat, statc."""
    categories = await db.list_categories()
    if not categories:
        await message.answer(
            f"Hozircha kategoriyalar yo'q. Avval «{BTN_ADD_CATEGORY}» bilan qo'shing.",
            reply_markup=_kb_menu_only() if include_menu else None,
        )
        return False
    chunk, page, total_pages, _total = _page_slice(categories, page)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"🏛 {c['name'][:52]}",
                callback_data=f"{prefix}:{c['id']}",
            )
        ]
        for c in chunk
    ]
    nav_row = _pagination_row(
        f"navcat:{page - 1}:{prefix}" if page > 0 else None,
        f"navcat:{page + 1}:{prefix}" if page < total_pages - 1 else None,
        page,
        total_pages,
    )
    if nav_row:
        rows.append(nav_row)
    if extra_rows:
        rows.extend(extra_rows)
    if include_menu:
        rows.append(_row_menu())
    await _send_or_edit(
        message,
        _with_page_note(intro, page, total_pages),
        InlineKeyboardMarkup(inline_keyboard=rows),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )
    return True


async def _send_promo_categories_pick_nav(
    message: Message, prefix: str, page: int
) -> bool:
    """navcat:{page}:{prefix} — kontekstga qarab kerakli chaqiruvni tiklaydi."""
    base, _, rest = prefix.partition(":")
    if base == "agc":
        return await _send_promo_categories_pick(
            message,
            intro="🏛 <b>Kategoriyalar</b>\n\nTanlang:",
            prefix="agc",
            include_menu=True,
            page=page,
            edit=True,
        )
    if base == "apc":
        return await _send_promo_categories_pick(
            message,
            intro="🏛 <b>Kategoriyalar</b>\n\nTanlang:",
            prefix="apc",
            include_menu=True,
            page=page,
            edit=True,
        )
    if base == "pcat":
        return await _send_promo_categories_pick(
            message,
            intro="🎟 <b>Promolar</b>\n\nAvval kategoriyani tanlang:",
            prefix="pcat",
            page=page,
            edit=True,
        )
    if base == "gecs":
        try:
            group_id = int(rest)
        except ValueError:
            return False
        group = await db.get_group(group_id)
        if not group:
            return False
        return await _send_promo_categories_pick(
            message,
            intro=(
                f"📁 <b>{_h(group['name'])}</b>\n\n"
                "Guruh uchun <b>yangi kategoriyani</b> tanlang:"
            ),
            prefix=f"gecs:{group_id}",
            extra_rows=[
                [
                    InlineKeyboardButton(
                        text="◀️ Guruhga",
                        callback_data=f"pg:{group_id}",
                    )
                ]
            ],
            page=page,
            edit=True,
        )
    if base == "vgcs":
        try:
            group_id = int(rest)
        except ValueError:
            return False
        group = await db.get_group(group_id)
        if not group:
            return False
        return await _send_promo_categories_pick(
            message,
            intro=(
                f"📁 <b>{_h(group['name'])}</b>\n\n"
                "Guruh uchun <b>yangi kategoriyani</b> tanlang:"
            ),
            prefix=f"vgcs:{group_id}",
            extra_rows=[
                [
                    InlineKeyboardButton(
                        text="◀️ Guruhga",
                        callback_data=f"vg:{group_id}",
                    )
                ]
            ],
            page=page,
            edit=True,
        )
    if base == "pegc":
        try:
            promo_id = int(rest)
        except ValueError:
            return False
        promo = await db.get_promo(promo_id)
        if not promo:
            return False
        return await _send_promo_categories_pick(
            message,
            intro="Promoning yangi guruhini tanlash — avval kategoriya:",
            prefix=f"pegc:{promo_id}",
            page=page,
            edit=True,
        )
    if base == "qrc":
        try:
            link_id = int(rest)
        except ValueError:
            return False
        if not await db.get_link(link_id):
            return False
        extra = [
            [
                InlineKeyboardButton(
                    text="📥 Excel — barcha promo (QR + havolalar)",
                    callback_data=f"qrxlall:{link_id}",
                ),
            ],
            [
                InlineKeyboardButton(text="◀️ Link tanlash", callback_data="qr:bl"),
            ],
        ]
        return await _send_promo_categories_pick(
            message,
            intro=(
                "Avval <b>kategoriyani</b> tanlang.\n"
                "Barcha promo (~minglab) uchun Telegramda rasm yuborish sekin — "
                "to‘liq ro‘yxatni <b>Excel</b> dan oling."
            ),
            prefix=f"qrc:{link_id}",
            include_menu=True,
            extra_rows=extra,
            page=page,
            edit=True,
        )
    return False


async def _send_promo_groups_pick(
    message: Message,
    *,
    intro: str,
    prefix: str,
    category_id: int,
    back_callback: str | None = None,
    include_menu: bool = True,
    page: int = 0,
    edit: bool = False,
) -> bool:
    """category_id bo'yicha guruhlar. back_callback — kategoriyalar ro'yxatiga qaytish."""
    category = await db.get_category(category_id)
    if not category:
        await message.answer(
            "Kategoriya topilmadi.",
            reply_markup=_kb_menu_only() if include_menu else None,
        )
        return False
    groups = await db.list_groups(category_id=category_id)
    if not groups:
        rows: list[list[InlineKeyboardButton]] = []
        if back_callback:
            rows.append(
                [InlineKeyboardButton(text="◀️ Kategoriyalarga", callback_data=back_callback)]
            )
        if include_menu:
            rows.append(_row_menu())
        await _send_or_edit(
            message,
            f"🏛 <b>{_h(category['name'])}</b>\n\n"
            f"Bu kategoriyada guruhlar yo'q. «{BTN_ADD_GROUP}» bilan qo'shing.",
            InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
            edit=edit,
            parse_mode=ParseMode.HTML,
        )
        return False
    chunk, page, total_pages, _total = _page_slice(groups, page)
    rows = [
        [
            InlineKeyboardButton(
                text=f"📁 {g['name'][:52]}",
                callback_data=f"{prefix}:{g['id']}",
            )
        ]
        for g in chunk
    ]
    nav_row = _pagination_row(
        f"navgrp:{page - 1}:{category_id}:{prefix}" if page > 0 else None,
        f"navgrp:{page + 1}:{category_id}:{prefix}" if page < total_pages - 1 else None,
        page,
        total_pages,
    )
    if nav_row:
        rows.append(nav_row)
    if back_callback:
        rows.append(
            [InlineKeyboardButton(text="◀️ Kategoriyalarga", callback_data=back_callback)]
        )
    if include_menu:
        rows.append(_row_menu())
    cat_intro = (
        f"🏛 <b>{_h(category['name'])}</b>\n\n{intro}"
        if intro
        else f"🏛 <b>{_h(category['name'])}</b>\n\nGuruhni tanlang:"
    )
    await _send_or_edit(
        message,
        _with_page_note(cat_intro, page, total_pages),
        InlineKeyboardMarkup(inline_keyboard=rows),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )
    return True


async def _send_promo_groups_pick_nav(
    message: Message, category_id: int, prefix: str, page: int
) -> bool:
    """navgrp:{page}:{category_id}:{prefix} — kontekstga qarab chaqiruvni tiklaydi."""
    base, _, rest = prefix.partition(":")
    if base == "apg":
        return await _send_promo_groups_pick(
            message,
            intro="Guruhni tanlang:",
            prefix="apg",
            category_id=category_id,
            back_callback="apc:list",
            include_menu=True,
            page=page,
            edit=True,
        )
    if base == "pg":
        return await _send_promo_groups_pick(
            message,
            intro="Guruhni tanlang:",
            prefix="pg",
            category_id=category_id,
            back_callback="pcat:list",
            page=page,
            edit=True,
        )
    if base == "pegs":
        try:
            promo_id = int(rest)
        except ValueError:
            return False
        if not await db.get_promo(promo_id):
            return False
        return await _send_promo_groups_pick(
            message,
            intro="Yangi guruhni tanlang:",
            prefix=f"pegs:{promo_id}",
            category_id=category_id,
            back_callback=f"peg:{promo_id}",
            page=page,
            edit=True,
        )
    return False


def _kb_promo_detail(promo_id: int, group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Kodni o'zgartirish",
                    callback_data=f"pec:{promo_id}",
                ),
                InlineKeyboardButton(
                    text="📁 Guruhni almashtirish",
                    callback_data=f"peg:{promo_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Ro'yxatga",
                    callback_data=f"pg:{group_id}",
                ),
                BTN_MENU,
            ],
        ]
    )


async def _send_promo_detail(message: Message, promo_id: int) -> None:
    row = await db.get_promo(promo_id)
    if not row:
        await message.answer("Promo topilmadi.", reply_markup=_kb_menu_only())
        return
    text = (
        f"<b>Promo #{promo_id}</b>\n"
        f"<b>Kod</b>: <code>{_h(row['code'])}</code>\n"
        f"<b>Guruh</b>: {_h(row['group_name'])}"
    )
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_promo_detail(promo_id, int(row["group_id"])),
    )


async def _send_promos_in_group(
    message: Message, group_id: int, *, page: int = 0, edit: bool = False
) -> None:
    group = await db.get_group(group_id)
    if not group:
        await message.answer("Guruh topilmadi.", reply_markup=_kb_menu_only())
        return
    category_id = int(group["category_id"])
    category = await db.get_category(category_id)
    cat_name = str(category["name"]) if category else "?"
    promos = await db.list_promos_by_group(group_id)
    back_to_groups = f"pcat:{category_id}"
    head = (
        f"📁 <b>{_h(group['name'])}</b>\n"
        f"🏛 Kategoriya: <b>{_h(cat_name)}</b>\n\n"
    )
    move_row = [
        InlineKeyboardButton(
            text="🏛 Kategoriyani almashtirish",
            callback_data=f"gec:{group_id}",
        )
    ]
    nav_rows: list[list[InlineKeyboardButton]] = [
        move_row,
        [
            InlineKeyboardButton(text="◀️ Guruhlarga", callback_data=back_to_groups),
            InlineKeyboardButton(text="◀️ Kategoriyalar", callback_data="pcat:list"),
        ],
        _row_menu(),
    ]
    if not promos:
        await _send_or_edit(
            message,
            head + "Bu guruhda promolar yo'q.",
            InlineKeyboardMarkup(inline_keyboard=nav_rows),
            edit=edit,
            parse_mode=ParseMode.HTML,
        )
        return
    chunk, page, total_pages, _total = _page_slice(promos, page)
    buttons = [
        [
            InlineKeyboardButton(
                text=p["code"][:60],
                callback_data=f"pp:{group_id}:{p['id']}",
            )
        ]
        for p in chunk
    ]
    pg_row = _pagination_row(
        f"navpromo:{page - 1}:{group_id}" if page > 0 else None,
        f"navpromo:{page + 1}:{group_id}" if page < total_pages - 1 else None,
        page,
        total_pages,
    )
    if pg_row:
        buttons.append(pg_row)
    buttons.extend(nav_rows)
    await _send_or_edit(
        message,
        _with_page_note(head + "Kerakli promoni tanlang:", page, total_pages),
        InlineKeyboardMarkup(inline_keyboard=buttons),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not message.from_user:
        return
    role = await get_role(message.from_user.id)
    if role is None:
        await message.answer("Bu bot faqat ruxsat berilgan foydalanuvchilar uchun.")
        return
    kb = await reply_kb_for(message.from_user.id)
    if role == access.ROLE_VIEWER:
        await message.answer(
            "Salom! Statistikani ko'rishingiz va Excel yuklab olishingiz mumkin.",
            reply_markup=kb,
        )
    else:
        await message.answer("Salom!", reply_markup=kb)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not message.from_user:
        return
    role = await get_role(message.from_user.id)
    if role is None:
        return
    if role == access.ROLE_VIEWER:
        await message.answer(
            f"<b>{BTN_STATS}</b> — sizga biriktirilgan kategoriyalar bo'yicha "
            "statistika va Excel export.",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_menu_only(),
        )
        return
    help_text = (
        f"1) Avval <b>{BTN_ADD_LINK}</b> va <b>{BTN_ADD_PROMO}</b> bilan boshlang.\n"
        f"2) <b>{BTN_ADD_CATEGORY}</b> — yangi kategoriya yaratish.\n"
        f"3) <b>{BTN_CATEGORIES}</b> — tartib (⬆️⬇️), nomini o'zgartirish, "
        f"guruhni boshqa kategoriyaga ko'chirish.\n"
        f"4) <b>{BTN_ADD_GROUP}</b> — avval kategoriya, keyin guruh nomi.\n"
        f"5) <b>{BTN_QR}</b> — link → kategoriya → guruh → promo yoki "
        f"guruhdagi/barcha promo uchun QR; tracking havolalar Excel ham mavjud.\n"
        f"6) <b>{BTN_STATS}</b> — kategoriya/guruh va yuklanishlar, Excel export.\n"
        f"7) Har bir link uchun QR markazidagi logotip — link qo'shganda yoki "
        f"<b>{BTN_LINKS}</b> → link kartochkasidagi <b>Logotipni almashtirish</b>.\n"
        f"8) <b>{BTN_LINKS}</b> — tahrir, logotip va o'chirish ham shu yerda.\n"
    )
    if role == access.ROLE_SUPER:
        help_text += (
            f"9) <b>{BTN_USERS}</b> — admin/viewer qo'shish (super faqat env'da).\n"
        )
    help_text += f"\nTracking URL ko'rinishi: <code>{_h(BASE_URL)}/r/token</code>"
    await message.answer(
        help_text,
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_menu_only(),
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await can_view_stats(message.from_user.id):
        return
    await state.clear()
    kb = await reply_kb_for(message.from_user.id)
    await message.answer("Bekor qilindi.", reply_markup=kb)


async def _deny_manage(message: Message) -> bool:
    """True = ruxsat yo'q (boshqaruv buyruqlari uchun)."""
    if message.from_user and await can_manage(message.from_user.id):
        return False
    if message.from_user and await can_view_stats(message.from_user.id):
        # Eski admin klaviaturasi qolgan viewer uchun tushunarli javob
        await message.answer(
            "Bu amal uchun ruxsat yo‘q. /start bosing — menyu yangilanadi.",
            reply_markup=await reply_kb_for(message.from_user.id),
        )
    return True


async def _deny_stats(message: Message) -> bool:
    if message.from_user and await can_view_stats(message.from_user.id):
        return False
    return True


@router.message(F.text == BTN_ADD_LINK)
async def add_link_prompt(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    await state.clear()
    await state.set_state(AddLinkStates.waiting_url)
    await message.answer(
        "To'liq URL yuboring (https://...).\nBekor qilish: /cancel",
        reply_markup=await _kb(message),
    )


@router.message(F.text == BTN_ADD_CATEGORY)
async def add_category_prompt(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    await state.clear()
    await state.set_state(AddCategoryStates.waiting_name)
    await message.answer(
        "Yangi <b>kategoriya</b> nomini yuboring (masalan: Asosiy).\n"
        "Bekor qilish: /cancel",
        parse_mode=ParseMode.HTML,
        reply_markup=await _kb(message),
    )


@router.message(AddCategoryStates.waiting_name, F.text & ~F.text.startswith("/"))
async def add_category_save(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Nom juda qisqa.")
        return
    try:
        cid = await db.add_category(name)
    except DuplicateError:
        await message.answer("Bu nom bilan kategoriya allaqachon mavjud.")
        return
    await state.clear()
    await message.answer(
        f"✅ Kategoriya saqlandi (id: <code>{cid}</code>).",
        parse_mode=ParseMode.HTML,
        reply_markup=await _kb(message),
    )


@router.message(F.text == BTN_ADD_GROUP)
async def add_group_prompt(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    await state.clear()
    await state.set_state(AddGroupStates.waiting_category)
    await message.answer(
        "Yangi guruh uchun avval <b>kategoriyani</b> tanlang:",
        parse_mode=ParseMode.HTML,
        reply_markup=await _kb(message),
    )
    await _send_promo_categories_pick(
        message,
        intro="🏛 <b>Kategoriyalar</b>\n\nTanlang:",
        prefix="agc",
        include_menu=True,
    )


@router.callback_query(F.data.startswith("agc:"))
async def add_group_pick_category(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        category_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    category = await db.get_category(category_id)
    if not category:
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    await state.update_data(add_group_category_id=category_id)
    await state.set_state(AddGroupStates.waiting_name)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            f"🏛 Tanlandi: <b>{_h(category['name'])}</b>\n\n"
            "Yangi promo <b>guruh nomini</b> yuboring.\nBekor qilish: /cancel",
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


@router.message(AddGroupStates.waiting_name, F.text & ~F.text.startswith("/"))
async def add_group_save(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    data = await state.get_data()
    category_id = data.get("add_group_category_id")
    if not isinstance(category_id, int):
        await state.clear()
        await message.answer(
            "Kategoriya tanlanmadi. Qaytadan boshlang.",
            reply_markup=await _kb(message),
        )
        return
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer(
            "Nom juda qisqa (kamida 2 belgi).\n"
            "Boshqa nom yuboring yoki /cancel"
        )
        return

    category = await db.get_category(category_id)
    if not category:
        await state.clear()
        await message.answer(
            f"Tanlangan kategoriya topilmadi. Qaytadan «{BTN_ADD_GROUP}» dan boshlang.",
            reply_markup=await _kb(message),
        )
        return

    try:
        gid = await db.add_group(name, category_id=category_id)
    except DuplicateError:
        existing = await db.get_group_by_name(name)
        if existing:
            other_cat = await db.get_category(int(existing["category_id"]))
            other_name = str(other_cat["name"]) if other_cat else "?"
            await message.answer(
                f"❌ <b>«{_h(name)}»</b> nomi allaqachon band.\n\n"
                f"U <b>{_h(other_name)}</b> kategoriyasida guruh sifatida mavjud "
                f"(id: <code>{int(existing['id'])}</code>).\n\n"
                "Guruh nomi <b>barcha kategoriyalar bo'yicha</b> unique bo'lishi kerak.\n"
                "Boshqa nom yuboring yoki /cancel",
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.answer(
                f"❌ <b>«{_h(name)}»</b> nomi allaqachon band "
                "(barcha kategoriyalar bo'yicha unique).\n"
                "Boshqa nom yuboring yoki /cancel",
                parse_mode=ParseMode.HTML,
            )
        return
    except Exception:
        # Holatni saqlab qolamiz — foydalanuvchi qayta urinishi mumkin
        logging.exception("add_group_save failed name=%r category_id=%s", name, category_id)
        await message.answer(
            "⚠️ Guruhni saqlashda xatolik bo'ldi (DB yoki tarmoq).\n"
            "Qayta urinib ko'ring yoki /cancel"
        )
        return

    await state.clear()
    await message.answer(
        f"✅ Guruh saqlandi.\n"
        f"🏛 {_h(category['name'])}\n"
        f"📁 {_h(name)} (id: <code>{gid}</code>)",
        parse_mode=ParseMode.HTML,
        reply_markup=await _kb(message),
    )


@router.message(AddLinkStates.waiting_url, F.text.in_(MAIN_MENU_TEXTS))
@router.message(AddLinkStates.optional_logo, F.text.in_(MAIN_MENU_TEXTS))
@router.message(AddPromoStates.waiting_group, F.text.in_(MAIN_MENU_TEXTS))
@router.message(AddPromoStates.waiting_code, F.text.in_(MAIN_MENU_TEXTS))
@router.message(AddCategoryStates.waiting_name, F.text.in_(MAIN_MENU_TEXTS))
@router.message(AddGroupStates.waiting_category, F.text.in_(MAIN_MENU_TEXTS))
@router.message(AddGroupStates.waiting_name, F.text.in_(MAIN_MENU_TEXTS))
@router.message(LogoForLinkStates.waiting_file, F.text.in_(MAIN_MENU_TEXTS))
@router.message(EditLinkStates.waiting_new_url, F.text.in_(MAIN_MENU_TEXTS))
@router.message(EditLinkStates.waiting_title, F.text.in_(MAIN_MENU_TEXTS))
@router.message(EditPromoStates.waiting_code, F.text.in_(MAIN_MENU_TEXTS))
@router.message(EditCategoryStates.waiting_name, F.text.in_(MAIN_MENU_TEXTS))
@router.message(EditGroupNameStates.waiting_name, F.text.in_(MAIN_MENU_TEXTS))
@router.message(UserManageStates.waiting_admin_id, F.text.in_(MAIN_MENU_TEXTS))
@router.message(UserManageStates.waiting_viewer_id, F.text.in_(MAIN_MENU_TEXTS))
async def fsm_to_main_menu(message: Message, state: FSMContext) -> None:
    """FSM ichida pastki menyu tugmalari — holatni tozalaydi va buyruqni bajaradi."""
    if not message.from_user:
        return
    text = message.text or ""
    if text == BTN_STATS:
        if await _deny_stats(message):
            return
        await state.clear()
        await stats_cmd(message)
        return
    if text == BTN_USERS:
        if not await can_manage_users(message.from_user.id):
            return
        await state.clear()
        await users_cmd(message, state)
        return
    if await _deny_manage(message):
        return
    await state.clear()
    if text == BTN_LINKS:
        await list_links_cmd(message)
    elif text == BTN_PROMOS:
        await list_promos_cmd(message)
    elif text == BTN_CATEGORIES:
        await manage_categories_cmd(message)
    elif text == BTN_QR:
        await qr_pick_link(message)
    elif text == BTN_ADD_LINK:
        await add_link_prompt(message, state)
    elif text == BTN_ADD_PROMO:
        await add_promo_prompt(message, state)
    elif text == BTN_ADD_CATEGORY:
        await add_category_prompt(message, state)
    elif text == BTN_ADD_GROUP:
        await add_group_prompt(message, state)


@router.callback_query(F.data == "hm:main")
async def callback_home_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Inline menyudan pastki klaviaturaga qaytish."""
    if not callback.from_user or not await can_view_stats(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.clear()
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        kb = await reply_kb_for(callback.from_user.id)
        await callback.message.answer("Pastki menyu:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "lb:list")
async def link_list_back(callback: CallbackQuery) -> None:
    """Link kartochkasidan ro'yxatga qaytish."""
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
    if await _deny_manage(message):
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
    if await _deny_manage(message):
        return
    await state.clear()
    await message.answer("Tahrir yakunlandi.", reply_markup=await _kb(message))


@router.message(EditLinkStates.waiting_title, F.text)
async def link_edit_save_title(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    data = await state.get_data()
    lid = data.get("edit_link_id")
    if not isinstance(lid, int):
        await state.clear()
        return
    title = (message.text or "").strip()
    await db.update_link_fields(lid, title=title if title else None)
    await state.clear()
    await message.answer("Sarlavha saqlandi.", reply_markup=await _kb(message))


@router.callback_query(F.data.startswith("ld:"))
async def link_delete_ask(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
    if await _deny_manage(message):
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
        reply_markup=await _kb(message),
    )


@router.message(AddLinkStates.optional_logo, Command("skip"))
async def add_link_skip_logo(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    await state.clear()
    await message.answer("Logotipsiz saqlandi.", reply_markup=await _kb(message))


@router.message(AddLinkStates.optional_logo, F.text)
async def add_link_logo_hint(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    await message.answer(
        "Logotipni <b>surat</b> yoki <b>dokument</b> (PNG/JPEG) sifatida yuboring, yoki /skip",
        parse_mode=ParseMode.HTML,
    )


@router.message(AddLinkStates.optional_logo, F.document | F.photo)
async def add_link_save_logo(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
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
    await message.answer("✅", reply_markup=await _kb(message))


@router.message(F.text == BTN_ADD_PROMO)
async def add_promo_prompt(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    await state.clear()
    await state.set_state(AddPromoStates.waiting_group)
    await message.answer(
        "Yangi promo uchun avval kategoriyani tanlang:",
        reply_markup=await _kb(message),
    )
    await _send_promo_categories_pick(
        message,
        intro="🏛 <b>Kategoriyalar</b>\n\nTanlang:",
        prefix="apc",
        include_menu=True,
    )


@router.callback_query(F.data == "apc:list")
async def add_promo_categories_back(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.set_state(AddPromoStates.waiting_group)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promo_categories_pick(
            callback.message,
            intro="🏛 <b>Kategoriyalar</b>\n\nTanlang:",
            prefix="apc",
            include_menu=True,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("apc:"))
async def add_promo_pick_category(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        category_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_category(category_id):
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    await state.update_data(add_promo_category_id=category_id)
    await state.set_state(AddPromoStates.waiting_group)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promo_groups_pick(
            callback.message,
            intro="Guruhni tanlang:",
            prefix="apg",
            category_id=category_id,
            back_callback="apc:list",
            include_menu=True,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("apg:"))
async def add_promo_pick_group(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        group_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    group = await db.get_group(group_id)
    if not group:
        await callback.answer("Guruh topilmadi", show_alert=True)
        return
    data = await state.get_data()
    expected_category_id = data.get("add_promo_category_id")
    if (
        not isinstance(expected_category_id, int)
        or int(group["category_id"]) != expected_category_id
    ):
        await callback.answer(
            "Guruh tanlangan kategoriyaga tegishli emas. Qaytadan kategoriyani tanlang.",
            show_alert=True,
        )
        return
    await state.update_data(add_promo_group_id=group_id)
    await state.set_state(AddPromoStates.waiting_code)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            f"📁 Tanlandi: <b>{_h(group['name'])}</b>\n\n"
            "Promo kod matnini yuboring (masalan: SUMMER2026).",
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


@router.message(AddPromoStates.waiting_code, F.text & ~F.text.startswith("/"))
async def add_promo_save(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    data = await state.get_data()
    group_id = data.get("add_promo_group_id")
    if not isinstance(group_id, int):
        await state.clear()
        await message.answer("Guruh tanlanmadi. Qaytadan boshlang.", reply_markup=await _kb(message))
        return
    code = (message.text or "").strip()
    if len(code) < 2:
        await message.answer("Kod juda qisqa.")
        return
    try:
        pid = await db.add_promo(code, group_id)
    except ValueError:
        await state.clear()
        await message.answer("Guruh topilmadi. Qaytadan urinib ko'ring.", reply_markup=await _kb(message))
        return
    except DuplicateError:
        await message.answer("Bu guruhda bunday promo allaqachon mavjud.")
        return
    await state.clear()
    await message.answer(f"✅ Promo saqlandi (id: {pid}).", reply_markup=await _kb(message))


@router.message(F.text == BTN_LINKS)
async def list_links_cmd(message: Message) -> None:
    if await _deny_manage(message):
        return
    await _show_links_list_message(
        message,
        "📋 <b>Linklar</b>\n\nTanlang — keyin logotip va tahrir tugmalari chiqadi:",
    )


@router.message(F.text == BTN_PROMOS)
async def list_promos_cmd(message: Message) -> None:
    if await _deny_manage(message):
        return
    await _send_promo_categories_pick(
        message,
        intro="🎟 <b>Promolar</b>\n\nAvval kategoriyani tanlang:",
        prefix="pcat",
    )


@router.callback_query(F.data == "pcat:list")
async def promo_categories_list_back(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promo_categories_pick(
            callback.message,
            intro="🎟 <b>Promolar</b>\n\nAvval kategoriyani tanlang:",
            prefix="pcat",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("pcat:"))
async def promo_category_open(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        category_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_category(category_id):
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promo_groups_pick(
            callback.message,
            intro="Guruhni tanlang:",
            prefix="pg",
            category_id=category_id,
            back_callback="pcat:list",
        )
    await callback.answer()


@router.callback_query(F.data == "pg:list")
async def promo_groups_list_back(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promo_categories_pick(
            callback.message,
            intro="🎟 <b>Promolar</b>\n\nAvval kategoriyani tanlang:",
            prefix="pcat",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("pg:"))
async def promo_group_open(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        group_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promos_in_group(callback.message, group_id)
    await callback.answer()


@router.callback_query(F.data.startswith("gec:"))
async def group_edit_category_start(callback: CallbackQuery) -> None:
    """Guruhni boshqa kategoriyaga ko'chirish — kategoriyalar ro'yxati."""
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        group_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    group = await db.get_group(group_id)
    if not group:
        await callback.answer("Guruh topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promo_categories_pick(
            callback.message,
            intro=(
                f"📁 <b>{_h(group['name'])}</b>\n\n"
                "Guruh uchun <b>yangi kategoriyani</b> tanlang:"
            ),
            prefix=f"gecs:{group_id}",
            extra_rows=[
                [
                    InlineKeyboardButton(
                        text="◀️ Guruhga",
                        callback_data=f"pg:{group_id}",
                    )
                ]
            ],
        )
    await callback.answer()


@router.callback_query(F.data.startswith("gecs:"))
async def group_edit_category_save(callback: CallbackQuery) -> None:
    """Tanlangan kategoriyaga guruhni ko'chiradi."""
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        group_id = int(parts[1])
        category_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_group(group_id):
        await callback.answer("Guruh topilmadi", show_alert=True)
        return
    if not await db.get_category(category_id):
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    try:
        ok = await db.update_group_category(group_id, category_id)
    except DuplicateError:
        await callback.answer(
            "Bu nom bilan guruh allaqachon mavjud",
            show_alert=True,
        )
        return
    if not ok:
        await callback.answer("Saqlab bo'lmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promos_in_group(callback.message, group_id)
    await callback.answer("Kategoriya yangilandi")


# ─── Kategoriya / guruh boshqaruvi (🏛 Kategoriyalar) ─────────────────────────


async def _send_manage_categories_list(
    message: Message, *, page: int = 0, edit: bool = False
) -> None:
    categories = await db.list_categories()
    if not categories:
        await message.answer(
            f"Hozircha kategoriyalar yo'q. «{BTN_ADD_CATEGORY}» bilan qo'shing.",
            reply_markup=_kb_menu_only(),
        )
        return
    chunk, page, total_pages, total = _page_slice(categories, page)
    start = page * LIST_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for i, c in enumerate(chunk):
        pos = start + i + 1
        cid = int(c["id"])
        name = str(c["name"])
        # Nom tugmasi: ▲▼ qisqa — nomga ko'proq joy
        label = f"{pos}. {name}"
        if len(label) > 56:
            label = label[:55] + "…"
        row = [
            InlineKeyboardButton(
                text="▲",
                callback_data=f"vcmp:{cid}:u:{page}",
            ),
            InlineKeyboardButton(
                text="▼",
                callback_data=f"vcmp:{cid}:d:{page}",
            ),
            InlineKeyboardButton(
                text=label,
                callback_data=f"vc:{cid}",
            ),
        ]
        rows.append(row)
    nav_row = _pagination_row(
        f"navvc:{page - 1}" if page > 0 else None,
        f"navvc:{page + 1}" if page < total_pages - 1 else None,
        page,
        total_pages,
    )
    if nav_row:
        rows.append(nav_row)
    rows.append(_row_menu())
    await _send_or_edit(
        message,
        _with_page_note(
            "🏛 <b>Kategoriyalar</b>\n\n"
            f"Jami: <b>{total}</b>\n"
            "▲▼ — tartib; nom — tahrirlash:",
            page,
            total_pages,
        ),
        InlineKeyboardMarkup(inline_keyboard=rows),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )


async def _send_manage_category_detail(
    message: Message, category_id: int, *, page: int = 0, edit: bool = False
) -> None:
    category = await db.get_category(category_id)
    if not category:
        await message.answer("Kategoriya topilmadi.", reply_markup=_kb_menu_only())
        return
    groups = await db.list_groups(category_id=category_id)
    chunk, page, total_pages, _total = _page_slice(groups, page)
    categories = await db.list_categories()
    cat_ids = [int(c["id"]) for c in categories]
    try:
        cat_pos = cat_ids.index(category_id) + 1
    except ValueError:
        cat_pos = int(category.get("priority") or 0)
    cat_total = len(categories)

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="⬆️ Yuqoriga",
                callback_data=f"vcmd:{category_id}:u",
            ),
            InlineKeyboardButton(
                text="⬇️ Pastga",
                callback_data=f"vcmd:{category_id}:d",
            ),
        ],
        [
            InlineKeyboardButton(
                text="✏️ Nomini o'zgartirish",
                callback_data=f"vcr:{category_id}",
            )
        ],
    ]
    for g in chunk:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📁 {g['name'][:52]}",
                    callback_data=f"vg:{g['id']}",
                )
            ]
        )
    nav_row = _pagination_row(
        f"navvg:{page - 1}:{category_id}" if page > 0 else None,
        f"navvg:{page + 1}:{category_id}" if page < total_pages - 1 else None,
        page,
        total_pages,
    )
    if nav_row:
        rows.append(nav_row)
    rows.append(
        [
            InlineKeyboardButton(
                text="◀️ Kategoriyalar",
                callback_data="vl:list",
            )
        ]
    )
    rows.append(_row_menu())
    body = (
        f"🏛 <b>{_h(category['name'])}</b>\n"
        f"Tartib: <b>{cat_pos}</b> / {cat_total}\n\n"
        f"Guruhlar: <b>{len(groups)}</b>\n"
    )
    if groups:
        body += "\nGuruhni tanlang yoki nomini o'zgartiring:"
    else:
        body += f"\nBu kategoriyada guruh yo'q. «{BTN_ADD_GROUP}» bilan qo'shing."
    await _send_or_edit(
        message,
        _with_page_note(body, page, total_pages),
        InlineKeyboardMarkup(inline_keyboard=rows),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )


async def _send_manage_group_detail(message: Message, group_id: int) -> None:
    group = await db.get_group(group_id)
    if not group:
        await message.answer("Guruh topilmadi.", reply_markup=_kb_menu_only())
        return
    category_id = int(group["category_id"])
    category = await db.get_category(category_id)
    cat_name = str(category["name"]) if category else "?"
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="✏️ Nomini o'zgartirish",
                callback_data=f"vgr:{group_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="🏛 Kategoriyani almashtirish",
                callback_data=f"vgc:{group_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="◀️ Guruhlar",
                callback_data=f"vc:{category_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="◀️ Kategoriyalar",
                callback_data="vl:list",
            )
        ],
        _row_menu(),
    ]
    await message.answer(
        f"📁 <b>{_h(group['name'])}</b>\n"
        f"🏛 Kategoriya: <b>{_h(cat_name)}</b>\n\n"
        "Nima qilamiz?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(F.text == BTN_CATEGORIES)
async def manage_categories_cmd(message: Message) -> None:
    if await _deny_manage(message):
        return
    await _send_manage_categories_list(message)


@router.callback_query(F.data == "vl:list")
async def manage_categories_list_back(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_manage_categories_list(callback.message)
    await callback.answer()


def _parse_category_move(data: str) -> tuple[int, int, int | None] | None:
    """vcmp:id:u|d:page yoki vcmd:id:u|d → (category_id, direction, page|None)."""
    parts = (data or "").split(":")
    if len(parts) < 3:
        return None
    try:
        category_id = int(parts[1])
    except ValueError:
        return None
    flag = parts[2]
    if flag == "u":
        direction = -1
    elif flag == "d":
        direction = 1
    else:
        return None
    page: int | None = None
    if len(parts) >= 4:
        try:
            page = int(parts[3])
        except ValueError:
            return None
    return category_id, direction, page


@router.callback_query(F.data.startswith("vcmp:"))
async def manage_category_move_list(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parsed = _parse_category_move(callback.data or "")
    if not parsed:
        await callback.answer("Xato", show_alert=True)
        return
    category_id, direction, page = parsed
    if page is None:
        page = 0
    moved = await db.move_category_priority(category_id, direction=direction)
    if not moved:
        tip = "Allaqachon birinchi" if direction < 0 else "Allaqachon oxirgi"
        await callback.answer(tip, show_alert=False)
        return
    if callback.message:
        await _send_manage_categories_list(
            callback.message, page=page, edit=True
        )
    await callback.answer("Tartib yangilandi")


@router.callback_query(F.data.startswith("vcmd:"))
async def manage_category_move_detail(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parsed = _parse_category_move(callback.data or "")
    if not parsed:
        await callback.answer("Xato", show_alert=True)
        return
    category_id, direction, _page = parsed
    moved = await db.move_category_priority(category_id, direction=direction)
    if not moved:
        tip = "Allaqachon birinchi" if direction < 0 else "Allaqachon oxirgi"
        await callback.answer(tip, show_alert=False)
        return
    if callback.message:
        await _send_manage_category_detail(
            callback.message, category_id, edit=True
        )
    await callback.answer("Tartib yangilandi")


@router.callback_query(F.data.startswith("vcr:"))
async def manage_category_rename_start(
    callback: CallbackQuery, state: FSMContext
) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        category_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    category = await db.get_category(category_id)
    if not category:
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    await state.set_state(EditCategoryStates.waiting_name)
    await state.update_data(edit_category_id=category_id)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            f"🏛 Hozirgi nom: <b>{_h(category['name'])}</b>\n\n"
            "Yangi kategoriya nomini yuboring.\nBekor: /cancel",
            parse_mode=ParseMode.HTML,
            reply_markup=await _kb(message),
        )
    await callback.answer()


@router.message(EditCategoryStates.waiting_name, F.text & ~F.text.startswith("/"))
async def manage_category_rename_save(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    data = await state.get_data()
    category_id = data.get("edit_category_id")
    if not isinstance(category_id, int):
        await state.clear()
        await message.answer("Qaytadan boshlang.", reply_markup=await _kb(message))
        return
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Nom juda qisqa.")
        return
    try:
        ok = await db.update_category(category_id, name=name)
    except DuplicateError:
        await message.answer("Bu nom bilan kategoriya allaqachon mavjud.")
        return
    await state.clear()
    if not ok:
        await message.answer("Kategoriya topilmadi.", reply_markup=await _kb(message))
        return
    await message.answer("✅ Kategoriya nomi yangilandi.", reply_markup=await _kb(message))
    await _send_manage_category_detail(message, category_id)


@router.callback_query(F.data.startswith("vc:"))
async def manage_category_open(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        category_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_category(category_id):
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_manage_category_detail(callback.message, category_id)
    await callback.answer()


@router.callback_query(F.data.startswith("vgr:"))
async def manage_group_rename_start(
    callback: CallbackQuery, state: FSMContext
) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        group_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    group = await db.get_group(group_id)
    if not group:
        await callback.answer("Guruh topilmadi", show_alert=True)
        return
    await state.set_state(EditGroupNameStates.waiting_name)
    await state.update_data(edit_group_id=group_id)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            f"📁 Hozirgi nom: <b>{_h(group['name'])}</b>\n\n"
            "Yangi guruh nomini yuboring.\nBekor: /cancel",
            parse_mode=ParseMode.HTML,
            reply_markup=await _kb(message),
        )
    await callback.answer()


@router.message(EditGroupNameStates.waiting_name, F.text & ~F.text.startswith("/"))
async def manage_group_rename_save(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    data = await state.get_data()
    group_id = data.get("edit_group_id")
    if not isinstance(group_id, int):
        await state.clear()
        await message.answer("Qaytadan boshlang.", reply_markup=await _kb(message))
        return
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Nom juda qisqa.")
        return
    try:
        ok = await db.update_group_fields(group_id, name=name)
    except DuplicateError:
        await message.answer("Bu nom bilan guruh allaqachon mavjud.")
        return
    await state.clear()
    if not ok:
        await message.answer("Guruh topilmadi.", reply_markup=await _kb(message))
        return
    await message.answer("✅ Guruh nomi yangilandi.", reply_markup=await _kb(message))
    await _send_manage_group_detail(message, group_id)


@router.callback_query(F.data.startswith("vg:"))
async def manage_group_open(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        group_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_group(group_id):
        await callback.answer("Guruh topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_manage_group_detail(callback.message, group_id)
    await callback.answer()


@router.callback_query(F.data.startswith("vgc:"))
async def manage_group_move_category_start(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        group_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    group = await db.get_group(group_id)
    if not group:
        await callback.answer("Guruh topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promo_categories_pick(
            callback.message,
            intro=(
                f"📁 <b>{_h(group['name'])}</b>\n\n"
                "Guruh uchun <b>yangi kategoriyani</b> tanlang:"
            ),
            prefix=f"vgcs:{group_id}",
            extra_rows=[
                [
                    InlineKeyboardButton(
                        text="◀️ Guruhga",
                        callback_data=f"vg:{group_id}",
                    )
                ]
            ],
        )
    await callback.answer()


@router.callback_query(F.data.startswith("vgcs:"))
async def manage_group_move_category_save(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        group_id = int(parts[1])
        category_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_group(group_id):
        await callback.answer("Guruh topilmadi", show_alert=True)
        return
    if not await db.get_category(category_id):
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    try:
        ok = await db.update_group_category(group_id, category_id)
    except DuplicateError:
        await callback.answer(
            "Bu nom bilan guruh allaqachon mavjud",
            show_alert=True,
        )
        return
    if not ok:
        await callback.answer("Saqlab bo'lmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_manage_group_detail(callback.message, group_id)
    await callback.answer("Kategoriya yangilandi")


@router.callback_query(F.data.startswith("pp:"))
async def promo_detail_open(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        promo_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promo_detail(callback.message, promo_id)
    await callback.answer()


@router.callback_query(F.data.startswith("pec:"))
async def promo_edit_code_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        promo_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    promo = await db.get_promo(promo_id)
    if not promo:
        await callback.answer("Promo topilmadi", show_alert=True)
        return
    await state.set_state(EditPromoStates.waiting_code)
    await state.update_data(edit_promo_id=promo_id)
    if callback.message:
        await callback.message.answer(
            "Yangi promo kodni yuboring.\nBekor: /cancel",
        )
    await callback.answer()


@router.message(EditPromoStates.waiting_code, F.text & ~F.text.startswith("/"))
async def promo_edit_code_save(message: Message, state: FSMContext) -> None:
    if await _deny_manage(message):
        return
    data = await state.get_data()
    promo_id = data.get("edit_promo_id")
    if not isinstance(promo_id, int):
        await state.clear()
        return
    code = (message.text or "").strip()
    if len(code) < 2:
        await message.answer("Kod juda qisqa.")
        return
    try:
        ok = await db.update_promo_fields(promo_id, code=code)
    except DuplicateError:
        await message.answer("Bu guruhda bunday promo allaqachon mavjud.")
        return
    await state.clear()
    if not ok:
        await message.answer("Promo topilmadi.", reply_markup=await _kb(message))
        return
    await _send_promo_detail(message, promo_id)
    await message.answer("✅", reply_markup=await _kb(message))


@router.callback_query(F.data.startswith("peg:"))
async def promo_edit_group_start(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        promo_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    promo = await db.get_promo(promo_id)
    if not promo:
        await callback.answer("Promo topilmadi", show_alert=True)
        return
    if callback.message:
        await _send_promo_categories_pick(
            callback.message,
            intro="Promoning yangi guruhini tanlash — avval kategoriya:",
            prefix=f"pegc:{promo_id}",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("pegc:"))
async def promo_edit_group_pick_category(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        promo_id = int(parts[1])
        category_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    promo = await db.get_promo(promo_id)
    if not promo:
        await callback.answer("Promo topilmadi", show_alert=True)
        return
    if not await db.get_category(category_id):
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promo_groups_pick(
            callback.message,
            intro="Yangi guruhni tanlang:",
            prefix=f"pegs:{promo_id}",
            category_id=category_id,
            back_callback=f"peg:{promo_id}",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("pegs:"))
async def promo_edit_group_save(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        promo_id = int(parts[1])
        group_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        ok = await db.update_promo_fields(promo_id, group_id=group_id)
    except DuplicateError:
        await callback.answer(
            "Bu guruhda shu promo kodi allaqachon bor",
            show_alert=True,
        )
        return
    if not ok:
        await callback.answer("Promo yoki guruh topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_promo_detail(callback.message, promo_id)
    await callback.answer("Saqlandi")


def _kb_stats_group_detail(category_id: int, group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📥 Excel — shu guruh",
                    callback_data=f"statxg:{group_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Guruhlar ro'yxati",
                    callback_data=f"statc:{category_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Kategoriyalar",
                    callback_data="stat:clist",
                )
            ],
            _row_menu(),
        ]
    )


def _filter_stats_by_scope(
    rows: list[dict],
    scope: set[int] | None,
    *,
    key: str = "category_id",
) -> list[dict]:
    if scope is None:
        return rows
    return [r for r in rows if int(r.get(key) or 0) in scope]


async def _show_stats_root(
    message: Message, *, user_id: int, page: int = 0, edit: bool = False
) -> None:
    """Statistika bosh sahifasi: kategoriyalar + jami yuklanish."""
    scope = await category_scope(user_id)
    if scope is not None and len(scope) == 0:
        await message.answer(
            "Sizga hali kategoriya biriktirilmagan.\n"
            "Super-admin «👥 Foydalanuvchilar» dan kamida 1 kategoriya belgilashi kerak.",
            reply_markup=_kb_menu_only(),
        )
        return
    totals = _filter_stats_by_scope(await db.stats_category_totals(), scope)
    if not totals:
        await message.answer(
            "Sizga biriktirilgan kategoriyalar yo'q yoki hali statistika bo'sh.",
            reply_markup=_kb_menu_only(),
        )
        return

    lines: list[str] = []
    total_clicks = 0
    for row in totals:
        clicks = int(row.get("clicks") or 0)
        total_clicks += clicks
        raw_name = str(row.get("category_name") or "")
        lines.append(f"{raw_name} — {clicks:,}".replace(",", " "))
    stats_block = "\n\n".join(lines)

    chunk, page, total_pages, _total = _page_slice(totals, page)
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=(
                    f"🏛 {str(row.get('category_name') or '')[:40]} — "
                    f"{int(row.get('clicks') or 0)}"
                ),
                callback_data=f"statc:{int(row['category_id'])}",
            )
        ]
        for row in chunk
    ]
    nav_row = _pagination_row(
        f"navstatc:{page - 1}" if page > 0 else None,
        f"navstatc:{page + 1}" if page < total_pages - 1 else None,
        page,
        total_pages,
    )
    if nav_row:
        buttons.append(nav_row)
    buttons.append(
        [
            InlineKeyboardButton(
                text="📥 Excel yuklash",
                callback_data="stat:xlsx",
            )
        ]
    )
    buttons.append(_row_menu())

    text = (
        "📊 <b>Yuklanishlar statistikasi</b>\n"
        "<i>Kategoriyalar bo'yicha</i>\n\n"
        f"{_h(stats_block)}\n\n"
        f"<b>Jami:</b> {_h(f'{total_clicks:,}'.replace(',', ' '))} ta yuklanish\n\n"
        "Kategoriyani tanlang:"
    )
    await _send_or_edit(
        message,
        _with_page_note(text, page, total_pages),
        InlineKeyboardMarkup(inline_keyboard=buttons),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )


async def _show_stats_groups_in_category(
    message: Message,
    category_id: int,
    *,
    user_id: int,
    page: int = 0,
    edit: bool = False,
) -> None:
    """Kategoriya ichidagi guruhlar + jami yuklanish."""
    if not await can_access_category(user_id, category_id):
        await message.answer("Bu kategoriyaga ruxsat yo'q.", reply_markup=_kb_menu_only())
        return
    category = await db.get_category(category_id)
    if not category:
        await message.answer("Kategoriya topilmadi.", reply_markup=_kb_menu_only())
        return

    all_totals = await db.stats_group_totals_desc()
    totals = [
        r for r in all_totals if int(r.get("category_id") or 0) == category_id
    ]

    lines: list[str] = []
    total_clicks = 0
    for row in totals:
        clicks = int(row.get("clicks") or 0)
        total_clicks += clicks
        raw_name = str(row.get("group_name") or "")
        lines.append(f"{raw_name} — {clicks:,}".replace(",", " "))
    stats_block = "\n\n".join(lines) if lines else "Bu kategoriyada guruhlar yo'q."

    chunk, page, total_pages, _total = _page_slice(totals, page)
    buttons: list[list[InlineKeyboardButton]] = []
    for row in chunk:
        gid = int(row["group_id"])
        gname = str(row.get("group_name") or "")
        clicks = int(row.get("clicks") or 0)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"📁 {gname[:40]} — {clicks}",
                    callback_data=f"statg:{gid}",
                )
            ]
        )
    nav_row = _pagination_row(
        f"navstatg:{page - 1}:{category_id}" if page > 0 else None,
        f"navstatg:{page + 1}:{category_id}" if page < total_pages - 1 else None,
        page,
        total_pages,
    )
    if nav_row:
        buttons.append(nav_row)
    buttons.append(
        [
            InlineKeyboardButton(
                text="📥 Excel — shu kategoriya",
                callback_data=f"statxc:{category_id}",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="◀️ Kategoriyalar",
                callback_data="stat:clist",
            )
        ]
    )
    buttons.append(_row_menu())

    footer = (
        "Guruhni tanlang:"
        if totals
        else "Bu kategoriyada guruhlar yo'q."
    )
    text = (
        "📊 <b>Statistika</b>\n"
        f"🏛 <b>{_h(category['name'])}</b>\n"
        "<i>Guruhlar bo'yicha</i>\n\n"
        f"{_h(stats_block)}\n\n"
        f"<b>Jami:</b> {_h(f'{total_clicks:,}'.replace(',', ' '))} ta yuklanish\n\n"
        f"{footer}"
    )
    await _send_or_edit(
        message,
        _with_page_note(text, page, total_pages),
        InlineKeyboardMarkup(inline_keyboard=buttons),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )


async def _send_stats_for_group_detail(
    message: Message, group_id: int, *, user_id: int
) -> None:
    if not await can_access_group(user_id, group_id):
        await message.answer("Bu guruhga ruxsat yo'q.", reply_markup=_kb_menu_only())
        return
    group = await db.get_group(group_id)
    if not group:
        await message.answer("Guruh topilmadi.", reply_markup=_kb_menu_only())
        return
    category_id = int(group["category_id"])
    category = await db.get_category(category_id)
    cat_name = str(category["name"]) if category else "?"
    stats = await db.stats_for_group(group_id)
    head = (
        "📊 <b>Statistika</b>\n"
        f"🏛 <b>{_h(cat_name)}</b>\n"
        f"📁 <b>{_h(group['name'])}</b>\n\n"
        "<b>Promo va jami yuklanishlar</b>\n"
    )
    if not stats:
        promo_lines = ["<i>Hozircha promo kodlar yo'q.</i>"]
        total_clicks = 0
    else:
        total_clicks = sum(int(s["clicks"] or 0) for s in stats)
        promo_lines = [
            f"• <code>{_h(s['promo_code'])}</code> — "
            f"<b>{int(s['clicks'] or 0)}</b> yuklanish"
            for s in stats
        ]
        promo_lines.append("")
        promo_lines.append(f"<b>Jami</b>: <b>{total_clicks}</b> yuklanish")
    parts = _split_html_lines(promo_lines)
    kb = _kb_stats_group_detail(category_id, group_id)
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


def _excel_sheet_title(name: str, used: set[str]) -> str:
    """Excel sheet nomi: max 31 belgi, taqiqlangan belgilarsiz, unique."""
    cleaned = "".join("_" if c in r'\/?*[]:' else c for c in (name or "").strip())
    cleaned = cleaned.strip() or "Kategoriya"
    base = cleaned[:31]
    title = base
    n = 2
    while title in used:
        suffix = f" ({n})"
        title = f"{base[: 31 - len(suffix)]}{suffix}"
        n += 1
    used.add(title)
    return title


def _build_stats_excel(
    category_rows: list[dict[str, object]],
    group_rows: list[dict[str, object]],
    promo_rows: list[dict[str, object]],
) -> bytes:
    """Kategoriyalar + Guruhlar + har bir kategoriya uchun alohida promo sheet."""
    from openpyxl.cell.cell import WriteOnlyCell
    from openpyxl.utils import get_column_letter

    wb = Workbook(write_only=True)
    used_titles: set[str] = set()

    def _bold_row(ws, values: list[object]) -> list:
        out = []
        for v in values:
            cell = WriteOnlyCell(ws, value=v)
            cell.font = Font(bold=True)
            out.append(cell)
        return out

    def _apply_widths(ws, headers: list[str], rows: list[list[object]]) -> None:
        widths = [max(12, len(h) + 2) for h in headers]
        for row in rows:
            for i, val in enumerate(row):
                if i >= len(widths):
                    break
                n = len(str(val if val is not None else ""))
                widths[i] = max(widths[i], min(n + 4, 70))
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def _write_sheet(
        title: str, headers: list[str], data: list[list[object]]
    ) -> None:
        ws = wb.create_sheet(_excel_sheet_title(title, used_titles))
        _apply_widths(ws, headers, data)
        ws.append(_bold_row(ws, headers))
        last = len(data) - 1
        for i, row in enumerate(data):
            ws.append(_bold_row(ws, row) if i == last else row)

    # --- Kategoriyalar ---
    cat_headers = ["Kategoriya", "Guruh soni", "Promo soni", "Yuklanishlar"]
    cat_data: list[list[object]] = []
    cat_groups = cat_promos = cat_clicks = 0
    for row in category_rows:
        g = int(row.get("group_count") or 0)
        p = int(row.get("promo_count") or 0)
        c = int(row.get("clicks") or 0)
        cat_groups += g
        cat_promos += p
        cat_clicks += c
        cat_data.append([str(row.get("category_name") or ""), g, p, c])
    cat_data.append(["Jami", cat_groups, cat_promos, cat_clicks])
    _write_sheet("Kategoriyalar", cat_headers, cat_data)

    # --- Guruhlar ---
    grp_headers = ["Kategoriya", "Guruh", "Promo soni", "Yuklanishlar"]
    grp_data: list[list[object]] = []
    grp_promos = grp_clicks = 0
    for row in group_rows:
        p = int(row.get("promo_count") or 0)
        c = int(row.get("clicks") or 0)
        grp_promos += p
        grp_clicks += c
        grp_data.append(
            [
                str(row.get("category_name") or ""),
                str(row.get("group_name") or ""),
                p,
                c,
            ]
        )
    grp_data.append(["", "Jami", grp_promos, grp_clicks])
    _write_sheet("Guruhlar", grp_headers, grp_data)

    # --- Har bir kategoriya: alohida sheet (promo kesimi) ---
    prm_headers = ["Guruh", "Promo kod", "Yuklanishlar"]
    # Tartib: category_rows prioriteti bo'yicha, so'ng promo_rows ichidagi ketma-ketlik
    by_cat: dict[str, list[dict[str, object]]] = {}
    for row in promo_rows:
        key = str(row.get("category_name") or "Noma'lum")
        by_cat.setdefault(key, []).append(row)

    cat_order: list[str] = []
    seen: set[str] = set()
    for row in category_rows:
        name = str(row.get("category_name") or "")
        if name and name not in seen:
            cat_order.append(name)
            seen.add(name)
    for name in by_cat:
        if name not in seen:
            cat_order.append(name)
            seen.add(name)

    for cat_name in cat_order:
        items = by_cat.get(cat_name) or []
        if not items:
            continue
        prm_data: list[list[object]] = []
        total = 0
        for item in items:
            c = int(item.get("clicks") or 0)
            total += c
            prm_data.append(
                [
                    str(item.get("group_name") or ""),
                    str(item.get("promo_code") or ""),
                    c,
                ]
            )
        prm_data.append(["", "Jami", total])
        _write_sheet(cat_name, prm_headers, prm_data)

    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _safe_stats_filename_part(name: str, max_len: int = 40) -> str:
    cleaned = "".join(
        c if (c.isalnum() or c in "._- ") else "_" for c in (name or "").strip()
    )
    cleaned = cleaned.strip(" ._") or "stat"
    return cleaned[:max_len]


async def _answer_stats_excel(
    message: Message,
    *,
    category_rows: list[dict[str, object]],
    group_rows: list[dict[str, object]],
    promo_rows: list[dict[str, object]],
    filename: str,
    caption: str,
) -> None:
    if not promo_rows and not group_rows and not category_rows:
        await message.answer("Statistika uchun ma'lumot yo'q.")
        return
    content = await asyncio.to_thread(
        _build_stats_excel, category_rows, group_rows, promo_rows
    )
    await message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption=caption,
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "stat:clist")
@router.callback_query(F.data == "stat:glist")
async def stats_back_to_categories(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_view_stats(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _show_stats_root(callback.message, user_id=callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("statc:"))
async def stats_open_category(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_view_stats(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        category_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await can_access_category(callback.from_user.id, category_id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if not await db.get_category(category_id):
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _show_stats_groups_in_category(
            callback.message, category_id, user_id=callback.from_user.id
        )
    await callback.answer()


@router.callback_query(F.data.startswith("statg:"))
async def stats_open_group(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_view_stats(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        group_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await can_access_group(callback.from_user.id, group_id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if not await db.get_group(group_id):
        await callback.answer("Guruh topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_stats_for_group_detail(
            callback.message, group_id, user_id=callback.from_user.id
        )
    await callback.answer()


@router.callback_query(F.data == "stat:xlsx")
async def stats_export_excel(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_view_stats(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await callback.answer("Tayyorlanmoqda…")
    scope = await category_scope(callback.from_user.id)
    category_rows, group_rows, promo_rows = await asyncio.gather(
        db.stats_category_totals(),
        db.stats_group_totals_desc(),
        db.stats_summary_by_group(),
    )
    category_rows = _filter_stats_by_scope(category_rows, scope)
    group_rows = _filter_stats_by_scope(group_rows, scope)
    promo_rows = _filter_stats_by_scope(promo_rows, scope)
    if not callback.message:
        return
    await _answer_stats_excel(
        callback.message,
        category_rows=category_rows,
        group_rows=group_rows,
        promo_rows=promo_rows,
        filename=f"statistika_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        caption="📥 Statistika: Kategoriyalar, Guruhlar va har bir kategoriya sheetlari.",
    )


@router.callback_query(F.data.startswith("statxc:"))
async def stats_export_category_excel(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_view_stats(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        category_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await can_access_category(callback.from_user.id, category_id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    category = await db.get_category(category_id)
    if not category:
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    await callback.answer("Tayyorlanmoqda…")
    category_rows, group_rows, promo_rows = await asyncio.gather(
        db.stats_category_totals(),
        db.stats_group_totals_desc(),
        db.stats_summary_by_group(),
    )
    category_rows = [
        r for r in category_rows if int(r.get("category_id") or 0) == category_id
    ]
    group_rows = [
        r for r in group_rows if int(r.get("category_id") or 0) == category_id
    ]
    promo_rows = [
        r for r in promo_rows if int(r.get("category_id") or 0) == category_id
    ]
    if not callback.message:
        return
    cat_name = str(category["name"])
    safe = _safe_stats_filename_part(cat_name)
    await _answer_stats_excel(
        callback.message,
        category_rows=category_rows,
        group_rows=group_rows,
        promo_rows=promo_rows,
        filename=(
            f"stat_{safe}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        ),
        caption=f"📥 Statistika — kategoriya: <b>{_h(cat_name)}</b>",
    )


@router.callback_query(F.data.startswith("statxg:"))
async def stats_export_group_excel(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_view_stats(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        group_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if not await can_access_group(callback.from_user.id, group_id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    group = await db.get_group(group_id)
    if not group:
        await callback.answer("Guruh topilmadi", show_alert=True)
        return
    await callback.answer("Tayyorlanmoqda…")
    category_id = int(group["category_id"])
    category_rows, group_rows, promo_rows = await asyncio.gather(
        db.stats_category_totals(),
        db.stats_group_totals_desc(),
        db.stats_summary_by_group(),
    )
    category_rows = [
        r for r in category_rows if int(r.get("category_id") or 0) == category_id
    ]
    # Kategoriya sheetida faqat shu guruhni hisobga olish uchun
    # category_rows dagi group/promo/clicks ni shu guruhdan qayta hisoblaymiz.
    group_rows = [r for r in group_rows if int(r.get("group_id") or 0) == group_id]
    promo_rows = [r for r in promo_rows if int(r.get("group_id") or 0) == group_id]
    if group_rows and category_rows:
        g0 = group_rows[0]
        category_rows = [
            {
                **category_rows[0],
                "group_count": 1,
                "promo_count": int(g0.get("promo_count") or 0),
                "clicks": int(g0.get("clicks") or 0),
            }
        ]
    elif category_rows:
        category_rows = [
            {
                **category_rows[0],
                "group_count": 0,
                "promo_count": 0,
                "clicks": 0,
            }
        ]
    if not callback.message:
        return
    gname = str(group["name"])
    cat = await db.get_category(category_id)
    cat_name = str(cat["name"]) if cat else "?"
    safe = _safe_stats_filename_part(gname)
    await _answer_stats_excel(
        callback.message,
        category_rows=category_rows,
        group_rows=group_rows,
        promo_rows=promo_rows,
        filename=(
            f"stat_{safe}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        ),
        caption=(
            f"📥 Statistika — 🏛 {_h(cat_name)} / 📁 <b>{_h(gname)}</b>"
        ),
    )


@router.message(F.text == BTN_STATS)
async def stats_cmd(message: Message) -> None:
    if await _deny_stats(message) or not message.from_user:
        return
    await _show_stats_root(message, user_id=message.from_user.id)


async def _send_qr_link_pick(
    target: Message, *, page: int = 0, edit: bool = False
) -> bool:
    """QR oqimi: link tanlash. False bo'lsa shartlar bajarilmagan."""
    links = await db.list_links()
    if not links:
        await target.answer(
            f"Avval «{BTN_ADD_LINK}» orqali link qo'shing.",
            reply_markup=_kb_menu_only(),
        )
        return False
    groups = await db.list_groups()
    if not groups:
        await target.answer(
            f"Avval promo <b>guruh</b> yarating yoki «{BTN_ADD_GROUP}»dan qo'shing.",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb_menu_only(),
        )
        return False
    promos = await db.list_promos()
    if not promos:
        await target.answer(
            f"Avval «{BTN_ADD_PROMO}» orqali promo qo'shing.",
            reply_markup=_kb_menu_only(),
        )
        return False
    chunk, page, total_pages, _total = _page_slice(links, page)
    buttons = [
        [
            InlineKeyboardButton(
                text=(l.get("title") or l["url"])[:60],
                callback_data=f"ql:{l['id']}",
            )
        ]
        for l in chunk
    ]
    nav_row = _pagination_row(
        f"navql:{page - 1}" if page > 0 else None,
        f"navql:{page + 1}" if page < total_pages - 1 else None,
        page,
        total_pages,
    )
    if nav_row:
        buttons.append(nav_row)
    buttons.append(_row_menu())
    await _send_or_edit(
        target,
        _with_page_note("Tracking uchun linkni tanlang:", page, total_pages),
        InlineKeyboardMarkup(inline_keyboard=buttons),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )
    return True


async def _send_qr_category_pick(target: Message, link_id: int) -> None:
    extra = [
        [
            InlineKeyboardButton(
                text="📥 Excel — barcha promo (QR + havolalar)",
                callback_data=f"qrxlall:{link_id}",
            ),
        ],
        [
            InlineKeyboardButton(text="◀️ Link tanlash", callback_data="qr:bl"),
        ],
    ]
    ok = await _send_promo_categories_pick(
        target,
        intro=(
            "Avval <b>kategoriyani</b> tanlang.\n"
            "Barcha promo (~minglab) uchun Telegramda rasm yuborish sekin — "
            "to‘liq ro‘yxatni <b>Excel</b> dan oling."
        ),
        prefix=f"qrc:{link_id}",
        include_menu=True,
        extra_rows=extra,
    )
    if not ok:
        return


async def _send_qr_group_pick(
    target: Message,
    link_id: int,
    category_id: int,
    *,
    page: int = 0,
    edit: bool = False,
) -> None:
    category = await db.get_category(category_id)
    if not category:
        await target.answer("Kategoriya topilmadi.", reply_markup=_kb_menu_only())
        return
    groups = await db.list_groups(category_id=category_id)
    chunk, page, total_pages, _total = _page_slice(groups, page)
    rows: list[list[InlineKeyboardButton]] = []
    for g in chunk:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📁 {g['name'][:52]}",
                    callback_data=f"qrg:{link_id}:{g['id']}",
                )
            ]
        )
    if not groups:
        rows.append(
            [
                InlineKeyboardButton(
                    text="(Bu kategoriyada guruh yo'q)",
                    callback_data=f"qrcl:{link_id}",
                )
            ]
        )
    nav_row = _pagination_row(
        f"navqrg:{page - 1}:{link_id}:{category_id}" if page > 0 else None,
        f"navqrg:{page + 1}:{link_id}:{category_id}"
        if page < total_pages - 1
        else None,
        page,
        total_pages,
    )
    if nav_row:
        rows.append(nav_row)
    rows.append(
        [
            InlineKeyboardButton(
                text="📥 Excel — shu kategoriya (QR + havolalar)",
                callback_data=f"qrxlc:{link_id}:{category_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="◀️ Kategoriyalar",
                callback_data=f"qrcl:{link_id}",
            ),
            BTN_MENU,
        ]
    )
    await _send_or_edit(
        target,
        _with_page_note(
            f"🏛 <b>{_h(category['name'])}</b>\n\n"
            "Promo <b>guruhini</b> tanlang yoki shu kategoriya uchun Excel oling:",
            page,
            total_pages,
        ),
        InlineKeyboardMarkup(inline_keyboard=rows),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )


async def _send_qr_promo_pick_for_group(
    target: Message,
    link_id: int,
    group_id: int,
    *,
    page: int = 0,
    edit: bool = False,
) -> None:
    group = await db.get_group(group_id)
    if not group:
        await target.answer("Guruh topilmadi.", reply_markup=_kb_menu_only())
        return
    promos = await db.list_promos_by_group(group_id)
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="📦 Shu guruhdagi barcha promo uchun QR",
                callback_data=f"qbulkg:{link_id}:{group_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📥 Excel — shu guruh (QR + havolalar)",
                callback_data=f"qrxlg:{link_id}:{group_id}",
            ),
        ],
    ]
    chunk, page, total_pages, _total = _page_slice(promos, page)
    for p in chunk:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=p["code"][:58],
                    callback_data=f"qp:{link_id}:{p['id']}:{group_id}",
                )
            ]
        )
    nav_row = _pagination_row(
        f"navqrp:{page - 1}:{link_id}:{group_id}" if page > 0 else None,
        f"navqrp:{page + 1}:{link_id}:{group_id}"
        if page < total_pages - 1
        else None,
        page,
        total_pages,
    )
    if nav_row:
        buttons.append(nav_row)
    buttons.append(
        [
            InlineKeyboardButton(
                text="◀️ Guruhlar",
                callback_data=f"qrgc:{link_id}:{group_id}",
            ),
            BTN_MENU,
        ]
    )
    text = _with_page_note(
        f"📁 <b>{_h(group['name'])}</b>\n\n"
        "Promo tanlang, guruhdagi barcha uchun QR yoki Excel:",
        page,
        total_pages,
    )
    await _send_or_edit(
        target,
        text,
        InlineKeyboardMarkup(inline_keyboard=buttons),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )


async def _answer_tracking_excel(
    message: Message,
    link_id: int,
    promos: list[dict[str, object]],
    *,
    caption: str,
) -> None:
    if not promos:
        await message.answer("Promo kodlar yo'q.")
        return
    link_row = await db.get_link(link_id)
    if not link_row:
        await message.answer("Link topilmadi.")
        return
    status = await message.answer(
        f"⏳ Excel tayyorlanmoqda: <b>{len(promos)}</b> ta promo (QR + havola)…"
        "\nBu biroz vaqt olishi mumkin.",
        parse_mode=ParseMode.HTML,
    )
    try:
        ids = [int(p["id"]) for p in promos]
        token_map = await db.ensure_track_tokens_for_promos(link_id, ids)
        rows_data: list[tuple[str, str, str, str]] = []
        skipped = 0
        for p in promos:
            pid = int(p["id"])
            token = token_map.get(pid)
            if not token:
                skipped += 1
                continue
            url = f"{BASE_URL}/r/{token}"
            rows_data.append(
                (
                    str(p.get("category_name") or ""),
                    str(p.get("group_name") or ""),
                    str(p.get("code") or ""),
                    url,
                )
            )
        if not rows_data:
            await message.answer("Tracking token yaratib bo'lmadi.")
            return
        content = await asyncio.to_thread(_build_tracking_links_excel, rows_data)
        filename = (
            f"qr_tracking_link{link_id}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
        cap = caption
        if skipped:
            cap = f"{caption}\n⚠️ {skipped} ta promo token ololmadi — tashlab ketildi."
        await message.answer_document(
            BufferedInputFile(content, filename=filename),
            caption=cap,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logging.exception("tracking Excel xato link_id=%s", link_id)
        await message.answer(
            "⚠️ Excel tayyorlashda xatolik bo'ldi. Qayta urinib ko'ring."
        )
    finally:
        try:
            await status.delete()
        except TelegramBadRequest:
            pass


async def _deliver_bulk_promo_qrs(
    message: Message,
    link_id: int,
    style: str,
    *,
    group_id: int,
    offset: int = 0,
) -> None:
    promos = await db.list_promos_by_group(group_id)
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
    if offset < 0:
        offset = 0
    if offset >= len(promos):
        await message.answer(
            "Barcha QR lar yuborilgan.",
            reply_markup=_kb_after_qr_batch(link_id, group_id),
        )
        return

    logo_override: str | None = None
    lp = (link_row.get("logo_path") or "").strip()
    if lp and Path(lp).is_file():
        logo_override = lp

    uslub = "Oddiy (oq-qora)" if style == "simple" else "Rangli"
    total = len(promos)
    batch = promos[offset : offset + BULK_QR_MAX_PHOTOS]
    batch_end = offset + len(batch)
    next_offset = batch_end if batch_end < total else None
    remaining = total - batch_end if next_offset is not None else 0

    await message.answer(
        f"📦 QR: <b>{offset + 1}–{batch_end}</b> / {total} "
        f"(<i>{uslub}</i>)…",
        parse_mode=ParseMode.HTML,
    )
    sent = 0
    try:
        for i, p in enumerate(batch):
            promo_id = p["id"]
            code = p["code"]
            token = await db.create_track_entry(link_id, promo_id)
            tracking_url = f"{BASE_URL}/r/{token}"
            png_bytes = await asyncio.to_thread(
                render_tracking_qr_png,
                tracking_url,
                logo_path=logo_override,
                style=style,
            )
            safe_name = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in code
            )[:40]
            file = BufferedInputFile(
                png_bytes, filename=f"qr_{safe_name or promo_id}.png"
            )
            last = i == len(batch) - 1
            await message.answer_photo(
                photo=file,
                caption=(
                    f"<b>Promo</b>: <code>{_h(code)}</code>\n"
                    f"<b>Uslub</b>: {uslub}\n"
                    f"<b>#{offset + i + 1}</b> / {total}\n"
                    f"{_caption_tracking_link(tracking_url)}"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_kb_after_qr_batch(
                    link_id,
                    group_id,
                    style=style,
                    next_offset=next_offset,
                    remaining=remaining,
                )
                if last
                else None,
            )
            sent += 1
            if not last:
                await asyncio.sleep(1)
    except TelegramBadRequest as e:
        logging.warning(
            "bulk QR to'xtadi (batch %s+%s, sent=%s): %s",
            offset,
            len(batch),
            sent,
            e,
        )
        await message.answer(
            f"⚠️ QR yuborish to'xtadi ({sent}/{len(batch)} shu partiyada).\n"
            "Birozdan keyin «Keyingisi» ni bosing yoki Excel dan foydalaning.",
            reply_markup=_kb_after_qr_batch(
                link_id,
                group_id,
                style=style,
                next_offset=offset + sent if offset + sent < total else None,
                remaining=max(0, total - (offset + sent)),
            ),
        )
        return
    except Exception:
        logging.exception("bulk QR xato (offset=%s sent=%s)", offset, sent)
        await message.answer(
            f"⚠️ QR yuborishda xatolik ({sent} ta yuborildi).\n"
            "Excel tugmasidan foydalaning yoki keyinroq qayta urinib ko'ring.",
            reply_markup=_kb_menu_only(),
        )
        return

@router.message(F.text == BTN_QR)
async def qr_pick_link(message: Message) -> None:
    if await _deny_manage(message):
        return
    await _send_qr_link_pick(message)


@router.callback_query(F.data == "qr:bl")
async def qr_back_to_links(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_qr_link_pick(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("qrcl:"))
async def qr_back_to_category_pick(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
        await _send_qr_category_pick(callback.message, link_id)
    await callback.answer()


@router.callback_query(F.data.startswith("qrc:"))
async def qr_open_category_for_groups(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        category_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id) or not await db.get_category(category_id):
        await callback.answer("Ma'lumot topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_qr_group_pick(callback.message, link_id, category_id)
    await callback.answer()


@router.callback_query(F.data.startswith("qrgc:"))
async def qr_back_to_groups_in_category(callback: CallbackQuery) -> None:
    """Promo tanlashdan guruhlar ro'yxatiga (shu kategoriya)."""
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        group_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    group = await db.get_group(group_id)
    if not await db.get_link(link_id) or not group:
        await callback.answer("Ma'lumot topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_qr_group_pick(
            callback.message, link_id, int(group["category_id"])
        )
    await callback.answer()


@router.callback_query(F.data.startswith("qrg:"))
async def qr_open_group_for_promos(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        group_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id) or not await db.get_group(group_id):
        await callback.answer("Ma'lumot topilmadi", show_alert=True)
        return
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _send_qr_promo_pick_for_group(callback.message, link_id, group_id)
    await callback.answer()


@router.callback_query(F.data.startswith("qbulkg:"))
async def qr_bulk_group_open_style_menu(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        group_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id) or not await db.get_group(group_id):
        await callback.answer("Ma'lumot topilmadi", show_alert=True)
        return
    promos = await db.list_promos_by_group(group_id)
    if not promos:
        await callback.answer("Guruhda promo yo'q", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _send_bulk_qr_style_prompt_group(
            callback.message, link_id, group_id
        )


@router.callback_query(F.data.startswith("qrxlall:"))
async def qr_excel_all_promos(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
    promos = await db.list_promos()
    if not promos:
        await callback.answer("Promo yo'q", show_alert=True)
        return
    await callback.answer("Excel tayyorlanmoqda…")
    if callback.message:
        await _answer_tracking_excel(
            callback.message,
            link_id,
            promos,
            caption=(
                "📥 Barcha promo uchun tracking havolalar (Excel).\n"
                "Har bir kategoriya — alohida sheet."
            ),
        )


@router.callback_query(F.data.startswith("qrxlc:"))
async def qr_excel_one_category(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        category_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id):
        await callback.answer("Link topilmadi", show_alert=True)
        return
    category = await db.get_category(category_id)
    if not category:
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    promos = await db.list_promos_by_category(category_id)
    if not promos:
        await callback.answer("Kategoriyada promo yo'q", show_alert=True)
        return
    await callback.answer("Excel tayyorlanmoqda…")
    if callback.message:
        await _answer_tracking_excel(
            callback.message,
            link_id,
            promos,
            caption=(
                f"📥 Kategoriya promo havolalari — <b>{_h(category['name'])}</b>"
            ),
        )


@router.callback_query(F.data.startswith("qrxlg:"))
async def qr_excel_one_group(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        group_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if not await db.get_link(link_id) or not await db.get_group(group_id):
        await callback.answer("Ma'lumot topilmadi", show_alert=True)
        return
    promos = await db.list_promos_by_group(group_id)
    if not promos:
        await callback.answer("Guruhda promo yo'q", show_alert=True)
        return
    await callback.answer("Excel tayyorlanmoqda…")
    if callback.message:
        g = await db.get_group(group_id)
        name = str((g or {}).get("name") or "")
        await _answer_tracking_excel(
            callback.message,
            link_id,
            promos,
            caption=f"📥 Guruh promo havolalari — <b>{_h(name)}</b>",
        )


@router.callback_query(F.data.startswith("qbulk:"))
async def qr_bulk_open_style_menu(callback: CallbackQuery) -> None:
    """Eski «barcha promo QR» — endi Excel ga yo'naltiriladi."""
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
        await callback.message.answer(
            "📦 Barcha promo uchun Telegramda rasm yuborish (~minglab) juda sekin.\n\n"
            "📥 «Excel — barcha promo» tugmasidan foydalaning.\n"
            "Bitta guruh uchun rasm-QR hali ham mavjud.",
            reply_markup=_kb_menu_only(),
        )


@router.callback_query(F.data.startswith("qall:"))
async def qr_all_promos_bulk(callback: CallbackQuery) -> None:
    """Eski global bulk — Excel ga yo'naltiriladi (guruh bulk: qallg:)."""
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
    await callback.answer(
        "Barcha promo uchun Excel dan foydalaning",
        show_alert=True,
    )
    if callback.message:
        await callback.message.answer(
            "📥 Barcha promo uchun «Excel — barcha promo» ni bosing.\n"
            "Guruh ichida «Shu guruhdagi barcha promo uchun QR» ishlayveradi.",
            reply_markup=_kb_menu_only(),
        )


@router.callback_query(F.data.startswith("qallg:"))
async def qr_all_promos_in_group_bulk(callback: CallbackQuery) -> None:
    """Tanlangan link + guruh uchun barcha promo QR."""
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[0] != "qallg":
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        group_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    mode = parts[3]
    if not await db.get_link(link_id) or not await db.get_group(group_id):
        await callback.answer("Ma'lumot topilmadi", show_alert=True)
        return
    if mode not in ("s", "r"):
        await callback.answer()
        if callback.message:
            await _send_bulk_qr_style_prompt_group(
                callback.message, link_id, group_id
            )
        return
    style = "simple" if mode == "s" else "styled"
    promos = await db.list_promos_by_group(group_id)
    if not promos:
        await callback.answer("Guruhda promo yo'q", show_alert=True)
        return
    await callback.answer("QRlar yuborilmoqda…")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _deliver_bulk_promo_qrs(
            callback.message, link_id, style, group_id=group_id, offset=0
        )


@router.callback_query(F.data.startswith("qmore:"))
async def qr_bulk_more(callback: CallbackQuery) -> None:
    """Keyingi 40 ta (yoki kamroq) QR partiyasi."""
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    # qmore:{link_id}:{group_id|0}:{s|r}:{offset}
    if len(parts) != 5 or parts[0] != "qmore":
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        gid = int(parts[2])
        mode = parts[3]
        offset = int(parts[4])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if mode not in ("s", "r") or offset < 0:
        await callback.answer("Xato", show_alert=True)
        return
    if gid == 0:
        await callback.answer(
            "Barcha promo uchun endi Excel ishlatiladi",
            show_alert=True,
        )
        return
    group_id = gid
    if not await db.get_link(link_id):
        await callback.answer("Link topilmadi", show_alert=True)
        return
    if not await db.get_group(group_id):
        await callback.answer("Guruh topilmadi", show_alert=True)
        return
    style = "simple" if mode == "s" else "styled"
    await callback.answer("Keyingi partiya…")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await _deliver_bulk_promo_qrs(
            callback.message,
            link_id,
            style,
            group_id=group_id,
            offset=offset,
        )


@router.callback_query(F.data.startswith("lg:"))
async def link_logo_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
    if await _deny_manage(message):
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
    await message.answer("✅", reply_markup=await _kb(message))


@router.callback_query(F.data.startswith("ql:"))
async def qr_pick_promo(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
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
        await _send_qr_category_pick(callback.message, link_id)
    await callback.answer()


@router.callback_query(F.data.startswith("qp:"))
async def qr_choose_style(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) not in (3, 4) or parts[0] != "qp":
        await callback.answer("Xato", show_alert=True)
        return
    try:
        link_id = int(parts[1])
        promo_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if len(parts) == 4:
        try:
            group_id = int(parts[3])
        except ValueError:
            await callback.answer("Xato", show_alert=True)
            return
        back_cd = f"qrg:{link_id}:{group_id}"
        back_txt = "◀️ Guruhdagi promolar"
    else:
        back_cd = f"qrcl:{link_id}"
        back_txt = "◀️ Kategoriyalar"

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
                            text=back_txt,
                            callback_data=back_cd,
                        ),
                    ],
                    _row_menu(),
                ]
            ),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("qst:"))
async def qr_build(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
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

    link_row = await db.get_link(link_id)
    if not link_row:
        await callback.answer("Link topilmadi", show_alert=True)
        return
    promo_label = await db.get_promo_code_by_id(promo_id)
    if not promo_label:
        await callback.answer("Promo topilmadi", show_alert=True)
        return

    try:
        token = await db.create_track_entry(link_id, promo_id)
    except Exception:
        logging.exception("qr_build track_entry link=%s promo=%s", link_id, promo_id)
        await callback.answer("Tracking yaratib bo'lmadi", show_alert=True)
        return
    tracking_url = f"{BASE_URL}/r/{token}"

    logo_override: str | None = None
    lp = (link_row.get("logo_path") or "").strip()
    if lp and Path(lp).is_file():
        logo_override = lp

    png_bytes = await asyncio.to_thread(
        render_tracking_qr_png, tracking_url, logo_path=logo_override, style=style
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
            f"{_caption_tracking_link(tracking_url)}"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_menu_only(),
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("Tayyor")


# ─── Ro'yxat sahifalash (nav*) ───────────────────────────────────────────


@router.callback_query(F.data.startswith("navll:"))
async def nav_links_list(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        page = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        await _show_links_list_message(
            callback.message,
            "📋 <b>Linklar</b>\n\nTanlang:",
            page=page,
            edit=True,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("navcat:"))
async def nav_categories(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    # navcat:{page}:{prefix...}
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        page = int(parts[1])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    prefix = parts[2]
    if callback.message:
        ok = await _send_promo_categories_pick_nav(
            callback.message, prefix, page
        )
        if not ok:
            await callback.answer("Ro'yxatni yangilab bo'lmadi", show_alert=True)
            return
    await callback.answer()


@router.callback_query(F.data.startswith("navgrp:"))
async def nav_groups(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    # navgrp:{page}:{category_id}:{prefix...}
    parts = callback.data.split(":", 3)
    if len(parts) < 4:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        page = int(parts[1])
        category_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    prefix = parts[3]
    if callback.message:
        ok = await _send_promo_groups_pick_nav(
            callback.message, category_id, prefix, page
        )
        if not ok:
            await callback.answer("Ro'yxatni yangilab bo'lmadi", show_alert=True)
            return
    await callback.answer()


@router.callback_query(F.data.startswith("navpromo:"))
async def nav_promos_in_group(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        page = int(parts[1])
        group_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        await _send_promos_in_group(
            callback.message, group_id, page=page, edit=True
        )
    await callback.answer()


@router.callback_query(F.data.startswith("navvc:"))
async def nav_manage_categories(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        page = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        await _send_manage_categories_list(
            callback.message, page=page, edit=True
        )
    await callback.answer()


@router.callback_query(F.data.startswith("navvg:"))
async def nav_manage_category_groups(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        page = int(parts[1])
        category_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        await _send_manage_category_detail(
            callback.message, category_id, page=page, edit=True
        )
    await callback.answer()


@router.callback_query(F.data.startswith("navstatc:"))
async def nav_stats_categories(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_view_stats(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        page = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        await _show_stats_root(
            callback.message,
            user_id=callback.from_user.id,
            page=page,
            edit=True,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("navstatg:"))
async def nav_stats_groups(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_view_stats(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        page = int(parts[1])
        category_id = int(parts[2])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if not await can_access_category(callback.from_user.id, category_id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    if callback.message:
        await _show_stats_groups_in_category(
            callback.message,
            category_id,
            user_id=callback.from_user.id,
            page=page,
            edit=True,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("navql:"))
async def nav_qr_links(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        page = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        await _send_qr_link_pick(callback.message, page=page, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("navqrg:"))
async def nav_qr_groups(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        page = int(parts[1])
        link_id = int(parts[2])
        category_id = int(parts[3])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        await _send_qr_group_pick(
            callback.message, link_id, category_id, page=page, edit=True
        )
    await callback.answer()


@router.callback_query(F.data.startswith("navqrp:"))
async def nav_qr_promos(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        page = int(parts[1])
        link_id = int(parts[2])
        group_id = int(parts[3])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    if callback.message:
        await _send_qr_promo_pick_for_group(
            callback.message, link_id, group_id, page=page, edit=True
        )
    await callback.answer()


# ── Super-admin: foydalanuvchilar (admin / viewer) ───────────────────────────


def _parse_telegram_id(raw: str) -> int | None:
    s = (raw or "").strip()
    if s.isdigit():
        value = int(s)
        return value if value > 0 else None
    return None


async def _push_role_keyboard(bot: Bot, telegram_id: int) -> bool:
    """Rol o'zgaganda target userga yangi pastki menyuni yuboradi.

    True — yuborildi. False — foydalanuvchi botni ochmagan/bloklagan
    (u holda /start bosishi kerak).
    """
    role = await get_role(telegram_id)
    try:
        if role is None:
            await bot.send_message(
                telegram_id,
                "Sizning botga kirish huquqingiz olib tashlandi.",
                reply_markup=ReplyKeyboardRemove(),
            )
        elif role == access.ROLE_VIEWER:
            await bot.send_message(
                telegram_id,
                "Rolingiz yangilandi: <b>viewer</b>.\n"
                "Endi faqat statistika va Excel mavjud.",
                parse_mode=ParseMode.HTML,
                reply_markup=await reply_kb_for(telegram_id),
            )
        elif role == access.ROLE_ADMIN:
            await bot.send_message(
                telegram_id,
                "Rolingiz yangilandi: <b>admin</b>.\n"
                "To‘liq boshqaruv menyusi ochildi.",
                parse_mode=ParseMode.HTML,
                reply_markup=await reply_kb_for(telegram_id),
            )
        else:
            await bot.send_message(
                telegram_id,
                "Menyu yangilandi.",
                reply_markup=await reply_kb_for(telegram_id),
            )
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logging.info(
            "Rol klaviaturasini yuborib bo'lmadi tid=%s: %s",
            telegram_id,
            exc,
        )
        return False


def _push_kb_note(ok: bool) -> str:
    if ok:
        return "\n<i>Foydalanuvchiga yangi menyu yuborildi.</i>"
    return (
        "\n<i>Menyu yuborilmadi — foydalanuvchi botni ochmagan yoki bloklagan. "
        "U /start bosishi kerak.</i>"
    )


async def _show_users_panel(message: Message, *, edit: bool = False) -> None:
    users = await db.list_bot_users()
    lines: list[str] = ["👥 <b>Foydalanuvchilar</b>\n"]
    if not users:
        lines.append("<i>Hali admin/viewer yo'q. Qo'lda qo'shing.</i>")
    else:
        for u in users:
            tid = int(u["telegram_id"])
            role = str(u["role"])
            name = str(u.get("display_name") or "").strip()
            label = f"{name} " if name else ""
            extra = ""
            if role == "viewer":
                cats = await db.list_viewer_categories(tid)
                if cats:
                    extra = " — " + ", ".join(str(c["name"]) for c in cats[:5])
                    if len(cats) > 5:
                        extra += "…"
                else:
                    extra = " — <i>kategoriya yo'q</i>"
            lines.append(
                f"• {label}<code>{tid}</code> — <b>{_h(role)}</b>{extra}"
            )
    lines.append(
        "\n<i>Super-admin faqat <code>ADMIN_IDS</code> (env) da — "
        "bu ro'yxatga kiritilmaydi.</i>"
    )
    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="➕ Admin", callback_data="usr:add:admin"),
            InlineKeyboardButton(text="➕ Viewer", callback_data="usr:add:viewer"),
        ]
    ]
    for u in users:
        tid = int(u["telegram_id"])
        role = str(u["role"])
        row = [
            InlineKeyboardButton(
                text=f"🗑 {tid}",
                callback_data=f"usr:del:{tid}",
            )
        ]
        if role == "viewer":
            row.insert(
                0,
                InlineKeyboardButton(
                    text=f"🏛 {tid}",
                    callback_data=f"usr:cats:{tid}",
                ),
            )
        buttons.append(row)
    buttons.append(_row_menu())
    text = "\n".join(lines)
    await _send_or_edit(
        message,
        text,
        InlineKeyboardMarkup(inline_keyboard=buttons),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text == BTN_USERS)
async def users_cmd(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await can_manage_users(message.from_user.id):
        return
    await state.clear()
    await _show_users_panel(message)


@router.callback_query(F.data == "usr:list")
async def users_list_cb(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await can_manage_users(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.clear()
    if callback.message:
        await _show_users_panel(callback.message, edit=True)
    await callback.answer()


@router.callback_query(F.data == "usr:add:admin")
async def users_add_admin_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await can_manage_users(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.set_state(UserManageStates.waiting_admin_id)
    if callback.message:
        await callback.message.answer(
            "Yangi <b>admin</b> Telegram ID sini yuboring (faqat raqam).\n"
            "Bekor: /cancel",
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


@router.callback_query(F.data == "usr:add:viewer")
async def users_add_viewer_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not await can_manage_users(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.set_state(UserManageStates.waiting_viewer_id)
    if callback.message:
        await callback.message.answer(
            "Yangi <b>viewer</b> Telegram ID sini yuboring (faqat raqam).\n"
            "Keyin kategoriyalarni tanlaysiz.\nBekor: /cancel",
            parse_mode=ParseMode.HTML,
        )
    await callback.answer()


@router.message(UserManageStates.waiting_admin_id, F.text & ~F.text.startswith("/"))
async def users_save_admin(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await can_manage_users(message.from_user.id):
        return
    tid = _parse_telegram_id(message.text or "")
    if tid is None:
        await message.answer("Noto'g'ri ID. Musbat butun son yuboring.")
        return
    if is_super_admin(tid):
        await message.answer(
            "Bu ID allaqachon env super-admin. DB ga qo'shish shart emas."
        )
        await state.clear()
        return

    existing = await db.get_bot_user(tid)
    await state.clear()
    if existing and existing["role"] == "admin":
        await message.answer(
            f"<code>{tid}</code> allaqachon <b>admin</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=await reply_kb_for(message.from_user.id),
        )
        return
    if existing and existing["role"] == "viewer":
        await message.answer(
            f"<code>{tid}</code> hozir <b>viewer</b>.\n"
            "Admin qilinsinmi? Kategoriyalar o‘chadi.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Ha, admin qil",
                            callback_data=f"usr:ok:admin:{tid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ Bekor",
                            callback_data="usr:list",
                        )
                    ],
                ]
            ),
        )
        return

    await _apply_admin_role(message, tid, actor_id=message.from_user.id)


async def _apply_admin_role(
    message: Message, tid: int, *, actor_id: int
) -> None:
    await db.upsert_bot_user(tid, "admin")
    pushed = await _push_role_keyboard(message.bot, tid)
    kb = await reply_kb_for(actor_id)
    await message.answer(
        f"✅ Admin: <code>{tid}</code>{_push_kb_note(pushed)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    await _show_users_panel(message)


@router.message(UserManageStates.waiting_viewer_id, F.text & ~F.text.startswith("/"))
async def users_save_viewer(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await can_manage_users(message.from_user.id):
        return
    tid = _parse_telegram_id(message.text or "")
    if tid is None:
        await message.answer("Noto'g'ri ID. Musbat butun son yuboring.")
        return
    if is_super_admin(tid):
        await message.answer("Bu ID env super-admin — viewer qilib bo'lmaydi.")
        await state.clear()
        return

    existing = await db.get_bot_user(tid)
    await state.clear()
    if existing and existing["role"] == "viewer":
        await message.answer(
            f"<code>{tid}</code> allaqachon <b>viewer</b>.\n"
            "Kategoriyalarni shu yerda o‘zgartiring:",
            parse_mode=ParseMode.HTML,
            reply_markup=await reply_kb_for(message.from_user.id),
        )
        await _show_viewer_category_picker(message, tid)
        return
    if existing and existing["role"] == "admin":
        await message.answer(
            f"<code>{tid}</code> hozir <b>admin</b>.\n"
            "Viewer qilinsinmi? Keyin kamida 1 kategoriya tanlash shart.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Ha, viewer qil",
                            callback_data=f"usr:ok:viewer:{tid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ Bekor",
                            callback_data="usr:list",
                        )
                    ],
                ]
            ),
        )
        return

    await _start_viewer_setup(message, tid, actor_id=message.from_user.id)


async def _start_viewer_setup(
    message: Message, tid: int, *, actor_id: int
) -> None:
    """Viewer yaratadi; menyu faqat kategoriya tanlangandan keyin yuboriladi."""
    await db.upsert_bot_user(tid, "viewer")
    kb = await reply_kb_for(actor_id)
    await message.answer(
        f"✅ Viewer yaratildi: <code>{tid}</code>\n"
        "Kamida <b>1 kategoriya</b> tanlang, so‘ng <b>✅ Tayyor</b> ni bosing.\n"
        "<i>Tayyor bosilmaguncha foydalanuvchiga menyu yuborilmaydi.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    await _show_viewer_category_picker(message, tid)


@router.callback_query(F.data.startswith("usr:ok:admin:"))
async def users_confirm_admin(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage_users(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        tid = int(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if is_super_admin(tid):
        await callback.answer("Super-admin", show_alert=True)
        return
    if callback.message:
        await _apply_admin_role(
            callback.message, tid, actor_id=callback.from_user.id
        )
    await callback.answer()


@router.callback_query(F.data.startswith("usr:ok:viewer:"))
async def users_confirm_viewer(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage_users(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        tid = int(callback.data.split(":")[3])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if is_super_admin(tid):
        await callback.answer("Super-admin", show_alert=True)
        return
    if callback.message:
        await _start_viewer_setup(
            callback.message, tid, actor_id=callback.from_user.id
        )
    await callback.answer()


async def _show_viewer_category_picker(
    message: Message, telegram_id: int, *, edit: bool = False
) -> None:
    categories = await db.list_categories()
    selected = await db.list_viewer_category_ids(telegram_id)
    if not categories:
        await message.answer(
            "Avval kategoriya yarating, keyin viewer ga biriktiring.",
            reply_markup=_kb_menu_only(),
        )
        return
    buttons: list[list[InlineKeyboardButton]] = []
    for c in categories:
        cid = int(c["id"])
        mark = "✅ " if cid in selected else ""
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{str(c['name'])[:48]}",
                    callback_data=f"usr:tog:{telegram_id}:{cid}",
                )
            ]
        )
    n_sel = len(selected)
    buttons.append(
        [
            InlineKeyboardButton(
                text=f"✅ Tayyor ({n_sel})",
                callback_data=f"usr:done:{telegram_id}",
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="◀️ Foydalanuvchilar",
                callback_data="usr:list",
            )
        ]
    )
    buttons.append(_row_menu())
    warn = ""
    if n_sel == 0:
        warn = "\n\n⚠️ <b>Kamida 1 kategoriya tanlang</b> — aks holda stats bo‘sh."
    text = (
        f"🏛 Viewer <code>{telegram_id}</code> kategoriyalari\n"
        "Bosib yoqing/o‘chiring (bir nechta mumkin)."
        f"{warn}"
    )
    await _send_or_edit(
        message,
        text,
        InlineKeyboardMarkup(inline_keyboard=buttons),
        edit=edit,
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data.startswith("usr:done:"))
async def users_viewer_done(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage_users(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        tid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    user = await db.get_bot_user(tid)
    if not user or user["role"] != "viewer":
        await callback.answer("Viewer topilmadi", show_alert=True)
        return
    selected = await db.list_viewer_category_ids(tid)
    if not selected:
        await callback.answer("Kamida 1 kategoriya tanlang", show_alert=True)
        return
    pushed = await _push_role_keyboard(callback.bot, tid)
    await callback.answer("Saqlandi")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            f"✅ Viewer <code>{tid}</code> tayyor "
            f"({len(selected)} kategoriya).{_push_kb_note(pushed)}",
            parse_mode=ParseMode.HTML,
        )
        await _show_users_panel(callback.message)


@router.callback_query(F.data.startswith("usr:cats:"))
async def users_edit_viewer_cats(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage_users(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        tid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    user = await db.get_bot_user(tid)
    if not user or user["role"] != "viewer":
        await callback.answer("Viewer topilmadi", show_alert=True)
        return
    if callback.message:
        await _show_viewer_category_picker(callback.message, tid, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("usr:tog:"))
async def users_toggle_viewer_cat(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage_users(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Xato", show_alert=True)
        return
    try:
        tid = int(parts[2])
        category_id = int(parts[3])
    except ValueError:
        await callback.answer("Xato", show_alert=True)
        return
    user = await db.get_bot_user(tid)
    if not user or user["role"] != "viewer":
        await callback.answer("Viewer topilmadi", show_alert=True)
        return
    if not await db.get_category(category_id):
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    added = await db.toggle_viewer_category(tid, category_id)
    if callback.message:
        await _show_viewer_category_picker(callback.message, tid, edit=True)
    await callback.answer("Qo'shildi" if added else "Olib tashlandi")


@router.callback_query(F.data.startswith("usr:del:"))
async def users_delete_ask(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage_users(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        tid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if is_super_admin(tid):
        await callback.answer("Super-adminni o'chirib bo'lmaydi", show_alert=True)
        return
    user = await db.get_bot_user(tid)
    if not user:
        await callback.answer("Topilmadi", show_alert=True)
        return
    role = str(user["role"])
    if not callback.message:
        await callback.answer()
        return
    await callback.message.answer(
        f"🗑 <code>{tid}</code> ({_h(role)}) o‘chirilsinmi?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Ha, o‘chirish",
                        callback_data=f"usr:delok:{tid}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Bekor",
                        callback_data="usr:list",
                    )
                ],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("usr:delok:"))
async def users_delete_confirm(callback: CallbackQuery) -> None:
    if not callback.from_user or not await can_manage_users(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        tid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Xato", show_alert=True)
        return
    if is_super_admin(tid):
        await callback.answer("Super-adminni o'chirib bo'lmaydi", show_alert=True)
        return
    ok = await db.delete_bot_user(tid)
    if not ok:
        await callback.answer("Topilmadi", show_alert=True)
        return
    pushed = await _push_role_keyboard(callback.bot, tid)
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(
            f"🗑 <code>{tid}</code> o‘chirildi.{_push_kb_note(pushed)}",
            parse_mode=ParseMode.HTML,
        )
        await _show_users_panel(callback.message)
    await callback.answer("O'chirildi")


def get_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp


def make_bot() -> Bot:
    """Telegram API uchun uzoq timeout — tarmoq uzilishi / Windows ClientOSError kamayishi mumkin."""
    session = AiohttpSession(timeout=TELEGRAM_HTTP_TIMEOUT)
    return Bot(BOT_TOKEN, session=session)

