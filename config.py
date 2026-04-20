import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8080").strip().rstrip("/")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
# Render kabi platformalarda PORT avtomatik beriladi.
WEB_PORT = int(os.getenv("WEB_PORT") or os.getenv("PORT", "8080"))

# Telegram API so'rovlari (long polling) uchun sekundlarda; tarmoq sekin bo'lsa oshiring
TELEGRAM_HTTP_TIMEOUT = float(os.getenv("TELEGRAM_HTTP_TIMEOUT", "120"))

# --- Kuzatuv serveri (/r/...) xavfsizligi ---
# True bo'lsa, nginx/caddy orqasida birinchi proxy X-Forwarded-For ga ishonadi
TRUST_X_FORWARDED_FOR = os.getenv("TRUST_X_FORWARDED_FOR", "false").lower() in (
    "1",
    "true",
    "yes",
)
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
# Har bir IP uchun maksimal so'rovlar (sliding window ichida)
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "120"))
RATE_LIMIT_WINDOW_SEC = float(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))


def _parse_hex_rgb(raw: str | None, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if not raw:
        return default
    s = raw.strip()
    if len(s) == 7 and s.startswith("#"):
        try:
            return (
                int(s[1:3], 16),
                int(s[3:5], 16),
                int(s[5:7], 16),
            )
        except ValueError:
            pass
    return default


# QR ko'rinishi (yumaloq modul; logo ixtiyoriy)
QR_FOREGROUND_RGB = _parse_hex_rgb(
    os.getenv("QR_FOREGROUND_COLOR", "#7B4FE0"), (123, 79, 224)
)
QR_BACKGROUND_RGB = _parse_hex_rgb(
    os.getenv("QR_BACKGROUND_COLOR", "#FFFFFF"), (255, 255, 255)
)
# Rangli QR: markazdan chekkaga radial gradient. EDGE bo'lmasa QR_FOREGROUND ishlatiladi.
QR_GRADIENT_LEFT_RGB = _parse_hex_rgb(
    os.getenv("QR_GRADIENT_CENTER", os.getenv("QR_GRADIENT_LEFT", "#3B82F6")),
    (59, 130, 246),
)
QR_GRADIENT_RIGHT_RGB = _parse_hex_rgb(
    os.getenv("QR_GRADIENT_EDGE", os.getenv("QR_GRADIENT_RIGHT")),
    QR_FOREGROUND_RGB,
)
QR_LOGO_PATH = os.getenv("QR_LOGO_PATH", "").strip()
QR_LOGO_RATIO = float(os.getenv("QR_LOGO_RATIO", "0.22"))
QR_BOX_SIZE = int(os.getenv("QR_BOX_SIZE", "12"))
QR_BORDER = int(os.getenv("QR_BORDER", "2"))

_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = set()
for part in _raw_admins.replace(" ", "").split(","):
    if part.isdigit():
        ADMIN_IDS.add(int(part))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
