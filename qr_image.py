"""Tracking URL uchun QR: oddiy (oq-qora) yoki rangli (yumaloq modullar)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Literal

import qrcode
from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_M
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.colormasks import RadialGradiantColorMask, SolidFillColorMask
from qrcode.image.styles.moduledrawers import RoundedModuleDrawer, SquareModuleDrawer

from config import (
    QR_BACKGROUND_RGB,
    QR_BORDER,
    QR_BOX_SIZE,
    QR_GRADIENT_LEFT_RGB,
    QR_GRADIENT_RIGHT_RGB,
    QR_LOGO_PATH,
    QR_LOGO_RATIO,
)


def _effective_logo_path(logo_path: str | None) -> Path | None:
    if logo_path:
        p = Path(logo_path)
        if p.is_file():
            return p
    if QR_LOGO_PATH:
        p = Path(QR_LOGO_PATH)
        if p.is_file():
            return p
    return None


def render_tracking_qr_png(
    payload: str,
    logo_path: str | None = None,
    *,
    style: Literal["simple", "styled"] = "styled",
) -> bytes:
    """
    PNG baytlar.
    style=simple — klassik oq-qora kvadrat modullar; markazda logo qo'yilmaydi.
    style=styled — yumaloq modullar, markazdan chekkaga gradient (ko'k → binafsha);
        logo_path yoki .env QR_LOGO_PATH bo'lsa markazga logo.
    logo_path: link bo'yicha; bo'lmasa .env QR_LOGO_PATH (faqat styled uchun).
    """
    effective = _effective_logo_path(logo_path)
    if style == "simple":
        effective = None
    need_logo = effective is not None
    ec = ERROR_CORRECT_H if need_logo else ERROR_CORRECT_M

    if style == "simple":
        module_drawer = SquareModuleDrawer()
        color_mask = SolidFillColorMask(
            back_color=(255, 255, 255),
            front_color=(0, 0, 0),
        )
    else:
        module_drawer = RoundedModuleDrawer(radius_ratio=1)
        color_mask = RadialGradiantColorMask(
            back_color=QR_BACKGROUND_RGB,
            center_color=QR_GRADIENT_LEFT_RGB,
            edge_color=QR_GRADIENT_RIGHT_RGB,
        )

    kwargs: dict = {
        "image_factory": StyledPilImage,
        "module_drawer": module_drawer,
        "color_mask": color_mask,
    }
    if effective is not None:
        kwargs["embedded_image_path"] = str(effective)
        kwargs["embedded_image_ratio"] = QR_LOGO_RATIO

    qr = qrcode.QRCode(
        version=None,
        error_correction=ec,
        box_size=QR_BOX_SIZE,
        border=QR_BORDER,
    )
    qr.add_data(payload)
    qr.make(fit=True)

    img = qr.make_image(**kwargs)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
