"""Telegramdan kelgan suratni link uchun QR markazidagi logo fayliga saqlash."""

from __future__ import annotations

from io import BytesIO

from aiogram.types import Message
from PIL import Image

import db

_ALLOWED_DOC = frozenset({"image/png", "image/jpeg", "image/webp"})


async def download_and_save_link_logo(message: Message, link_id: int) -> str:
    """
    Faylni diskka yozadi, bazada logo_path ni yangilaydi.
    Raises ValueError — noto'g'ri format.
    """
    path = db.disk_path_for_link_logo(link_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    buf = BytesIO()
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if mime not in _ALLOWED_DOC:
            raise ValueError("Faqat PNG, JPEG yoki WebP (dokument sifatida).")
        await message.bot.download(message.document, destination=buf)
    elif message.photo:
        await message.bot.download(message.photo[-1], destination=buf)
    else:
        raise ValueError("Surat yoki dokument yuboring.")

    buf.seek(0)
    try:
        img = Image.open(buf).convert("RGBA")
    except OSError as e:
        raise ValueError("Rasmni o'qib bo'lmadi.") from e
    img.save(path, format="PNG")

    resolved = str(path.resolve())
    await db.set_link_logo_path(link_id, resolved)
    return resolved
