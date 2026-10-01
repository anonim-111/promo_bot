import logging
import secrets

import aiohttp
from aiohttp import ClientTimeout, web
from yarl import URL

from security_web import (
    SlidingWindowRateLimiter,
    get_client_ip,
    is_bot_or_crawler_ua,
    is_valid_track_token,
    rate_limit_middleware,
)

VISITOR_COOKIE_NAME = "pb_vid"
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 2  # 2 yil

# RFC 7230 hop-by-hop. Host ni upstream o'zi oladi; content-length ni javob tanasidan qo'yamiz.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
_PROXY_TIMEOUT = ClientTimeout(total=20, connect=10)


def _default_port(url: URL) -> int | None:
    if url.port is not None:
        return url.port
    if url.scheme == "https":
        return 443
    if url.scheme == "http":
        return 80
    return None


def resolve_proxy_upstream(raw: str, base_url: str) -> URL | None:
    """TRACK_PROXY_URL ni tekshiradi. Bo'sh, noto'g'ri yoki BASE_URL bilan bir xil bo'lsa None."""
    if not raw:
        return None
    upstream = URL(raw)
    if upstream.scheme not in ("http", "https") or not upstream.host:
        logging.error("TRACK_PROXY_URL noto'g'ri (http/https va host kerak): %s", raw)
        return None
    base = URL(base_url)
    same = (
        upstream.scheme == base.scheme
        and (upstream.host or "").lower() == (base.host or "").lower()
        and _default_port(upstream) == _default_port(base)
    )
    if same:
        logging.error(
            "TRACK_PROXY_URL BASE_URL bilan bir xil — proxy o'chirildi (loop): %s",
            raw,
        )
        return None
    return upstream.with_path("/").with_query(None).with_fragment(None)


def _proxy_request_headers(request: web.Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in _HOP_BY_HOP:
            continue
        headers[key] = value
    headers["X-Forwarded-For"] = get_client_ip(request)
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme).split(",")[0].strip()
    headers["X-Forwarded-Proto"] = proto or request.scheme
    headers["X-Forwarded-Host"] = request.host
    return headers


def _copy_response_headers(src: aiohttp.ClientResponse, dest: web.StreamResponse) -> None:
    for key, value in src.headers.items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP:
            continue
        if lowered == "set-cookie":
            dest.headers.add(key, value)
        else:
            dest.headers[key] = value


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


async def proxy_track(request: web.Request) -> web.StreamResponse:
    """/r/{token} ni boshqa serverga cookie va sarlavhalar bilan uzatadi.

    Upstream 302 (oxirgi promo URL) brauzerga o'zgartirilmasdan qaytadi.
    """
    token = request.match_info.get("token", "")
    if not is_valid_track_token(token):
        raise web.HTTPNotFound(text="Link topilmadi")

    upstream: URL = request.app["proxy_upstream"]
    dest = upstream.with_path(f"/r/{token}")
    if request.query_string:
        dest = dest.with_query(request.query_string)

    session: aiohttp.ClientSession = request.app["proxy_session"]
    try:
        async with session.get(
            dest,
            headers=_proxy_request_headers(request),
            allow_redirects=False,
            auto_decompress=False,
        ) as resp:
            body = await resp.read()
            out = web.Response(status=resp.status, body=body)
            _copy_response_headers(resp, out)
            return out
    except TimeoutError:
        logging.warning("track proxy timeout: %s", dest.with_query(None))
        raise web.HTTPGatewayTimeout(text="Upstream timeout") from None
    except aiohttp.ClientError:
        logging.exception("track proxy xatosi: %s", dest.with_query(None))
        raise web.HTTPBadGateway(text="Upstream unavailable") from None


async def track_handler(request: web.Request) -> web.StreamResponse:
    if request.app.get("proxy_upstream") is not None:
        return await proxy_track(request)
    return await redirect_handler(request)


async def _close_proxy_session(app: web.Application) -> None:
    session: aiohttp.ClientSession | None = app.get("proxy_session")
    if session is not None and not session.closed:
        await session.close()


def create_app() -> web.Application:
    from config import BASE_URL, RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SEC, TRACK_PROXY_URL

    app = web.Application(middlewares=[rate_limit_middleware])
    app["rate_limiter"] = SlidingWindowRateLimiter(
        max_requests=RATE_LIMIT_REQUESTS,
        window_seconds=RATE_LIMIT_WINDOW_SEC,
    )
    upstream = resolve_proxy_upstream(TRACK_PROXY_URL, BASE_URL)
    app["proxy_upstream"] = upstream
    if upstream is not None:
        app["proxy_session"] = aiohttp.ClientSession(timeout=_PROXY_TIMEOUT)
        app.on_cleanup.append(_close_proxy_session)
    app.router.add_get("/health", health)
    app.router.add_get("/r/{token}", track_handler)
    return app
