from aiohttp import web
from yarl import URL

from security_web import SlidingWindowRateLimiter, is_valid_track_token, rate_limit_middleware


async def redirect_handler(request: web.Request) -> web.StreamResponse:
    import db

    token = request.match_info.get("token", "")
    if not is_valid_track_token(token):
        raise web.HTTPNotFound(text="Link topilmadi")
    target = await db.get_link_url_by_token(token)
    if not target:
        raise web.HTTPNotFound(text="Link topilmadi")
    await db.increment_click(token)
    # Lotin bo'lmagan domen/yul uchun to'g'ri kodlangan Location
    raise web.HTTPFound(location=str(URL(target)))


def create_app() -> web.Application:
    from config import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SEC

    app = web.Application(middlewares=[rate_limit_middleware])
    app["rate_limiter"] = SlidingWindowRateLimiter(
        max_requests=RATE_LIMIT_REQUESTS,
        window_seconds=RATE_LIMIT_WINDOW_SEC,
    )
    app.router.add_get("/r/{token}", redirect_handler)
    return app
