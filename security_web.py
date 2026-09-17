"""
Kuzatuv (redirect) serveri uchun dasturiy himoya: tezlik cheklovi, token tekshiruvi.

Eslatma: katta hajmdagi DDoS ni to'liq to'xtatish odatda CDN (Cloudflare), firewall
va hosting provayderi darajasida qilinadi — bu yerda qo'llaniladigan choralarni
qo'shimcha himoya sifatida qabul qiling.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict

from aiohttp import web

from config import (
    RATE_LIMIT_ENABLED,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SEC,
    TRUST_X_FORWARDED_FOR,
)

# token_urlsafe + qisqartirish; noto'g'ri format DB ga yetib bormasligi uchun
TRACK_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")

# Link preview / crawler / uptime / AI — statistikaga kiritilmasin
# Eslatma: okhttp va yalang "bot" qo'shilmagan (real Android klientlar xavfi).
_BOT_UA_RE = re.compile(
    r"("
    # Ijtimoiy / link-preview
    r"TelegramBot|twitterbot|facebookexternalhit|Facebot|LinkedInBot|"
    r"Slackbot|Discordbot|WhatsApp|SkypeUriPreview|"
    r"Pinterest|VKShare|vkShare|"
    # Qidiruv / AI
    r"Googlebot|Google-Read-Aloud|bingbot|Baiduspider|YandexBot|DuckDuckBot|"
    r"Applebot|Slurp|ia_archiver|SemrushBot|AhrefsBot|"
    r"PetalBot|Bytespider|"
    r"GPTBot|ChatGPT-User|OAI-SearchBot|"
    r"ClaudeBot|Claude-Web|PerplexityBot|CCBot|"
    # Monitoring / uptime
    r"Pingdom|StatusCake|Site24x7|"
    # QR / scanner ilovalari (aniq nomlar)
    r"QR\s*Scanner|ZXing|"
    # Skript / headless
    r"HeadlessChrome|PhantomJS|curl/|wget/|python-requests|Go-http-client|"
    r"http\.rb|Java/|libwww-perl|Scrapy"
    r")",
    re.IGNORECASE,
)


def is_valid_track_token(token: str) -> bool:
    return bool(token and TRACK_TOKEN_RE.fullmatch(token))


def is_bot_or_crawler_ua(user_agent: str | None) -> bool:
    """Preview bot / crawler UA — click hisoblanmasin."""
    ua = (user_agent or "").strip()
    if not ua:
        return True
    return bool(_BOT_UA_RE.search(ua))


def get_client_ip(request: web.Request) -> str:
    if TRUST_X_FORWARDED_FOR:
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()[:200]
    return (request.remote or "0.0.0.0")[:200]


class SlidingWindowRateLimiter:
    """Har bir IP uchun so'rovlar soni (sliding window)."""

    __slots__ = ("_hits", "_max_req", "_window", "_prune_every", "_tick")

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._max_req = max(1, max_requests)
        self._window = max(1.0, window_seconds)
        self._prune_every = 0
        self._tick = 500

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        hits = self._hits[ip]
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= self._max_req:
            return False
        hits.append(now)

        self._prune_every += 1
        if self._prune_every >= self._tick:
            self._prune_every = 0
            dead = [k for k, v in self._hits.items() if not v]
            for k in dead:
                del self._hits[k]
        return True


@web.middleware
async def rate_limit_middleware(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    if not RATE_LIMIT_ENABLED or not request.path.startswith("/r/"):
        return await handler(request)

    limiter: SlidingWindowRateLimiter = request.app["rate_limiter"]
    ip = get_client_ip(request)
    if not limiter.allow(ip):
        return web.Response(
            status=429,
            text="Too Many Requests",
            headers={"Retry-After": str(int(RATE_LIMIT_WINDOW_SEC))},
        )
    return await handler(request)
