import secrets

from aiohttp import web
from yarl import URL

from security_web import (
    SlidingWindowRateLimiter,
    is_bot_or_crawler_ua,
    is_valid_track_token,
    rate_limit_middleware,
)

VISITOR_COOKIE_NAME = "pb_vid"
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 2  # 2 yil


async def health(request: web.Request) -> web.StreamResponse:
    """Render health check — /r/ kabi DB yuklamasiz."""
    return web.Response(text="OK")


async def redirect_handler(request: web.Request) -> web.StreamResponse:
    import db

    if not db.is_ready():
        raise web.HTTPServiceUnavailable(
            text="Server ishga tushmoqda, birozdan keyin qayta urinib ko'ring.",
            headers={"Retry-After": "3"},
        )

    token = request.match_info.get("token", "")
    if not is_valid_track_token(token):
        raise web.HTTPNotFound(text="Link topilmadi")
    target = await db.get_link_url_by_token(token)
    if not target:
        raise web.HTTPNotFound(text="Link topilmadi")

    ua = request.headers.get("User-Agent", "")
    count_visit = not is_bot_or_crawler_ua(ua)

    visitor_id = request.cookies.get(VISITOR_COOKIE_NAME)
    is_first_cookie = visitor_id is None
    if count_visit and is_first_cookie:
        visitor_id = secrets.token_urlsafe(16)

    if count_visit and visitor_id:
        await db.record_visit(token, visitor_id)

    # Lotin bo'lmagan domen/yul uchun to'g'ri kodlangan Location
    response = web.HTTPFound(location=str(URL(target)))
    if count_visit and is_first_cookie and visitor_id:
        from config import BASE_URL

        response.set_cookie(
            VISITOR_COOKIE_NAME,
            visitor_id,
            max_age=VISITOR_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
            secure=BASE_URL.lower().startswith("https://"),
        )
    raise response


def create_app() -> web.Application:
    from config import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SEC

    app = web.Application(middlewares=[rate_limit_middleware])
    app["rate_limiter"] = SlidingWindowRateLimiter(
        max_requests=RATE_LIMIT_REQUESTS,
        window_seconds=RATE_LIMIT_WINDOW_SEC,
    )
    app.router.add_get("/health", health)
    app.router.add_get("/r/{token}", redirect_handler)
    return app
