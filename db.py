import os
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
from asyncpg.exceptions import UniqueViolationError
from urllib.parse import urlparse

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_CATEGORY_NAME = "Сурхондарё вилояти"


class DuplicateError(Exception):
    """Unique constraint violation."""


CATEGORY_PRIORITY_ORDER: tuple[str, ...] = (
    "Тошкент шаҳри", "Тошкент вилояти", "Самарқанд", "Сирдарё", "Жиззах",
    "Бухоро", "Навоий", "Фарғона", "Андижон", "Наманган", "Сурхондарё",
    "Қашқадарё", "Хоразм", "Қорақалпоғистон",
)

_pool: asyncpg.Pool | None = None
_default_category_id_cache: int | None = None
_UNSET = object()


def _category_priority_for_name(name: str) -> int | None:
    name = (name or "").casefold().strip()
    for priority, prefix in enumerate(CATEGORY_PRIORITY_ORDER, start=1):
        if name == prefix.casefold() or name.startswith(prefix.casefold()):
            return priority
    return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _row(r: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    items = r.items() if hasattr(r, "items") else ((k, r[k]) for k in r.keys())  # type: ignore[union-attr]
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in items
    }


def _dsn() -> str:
    dsn = (
        os.getenv("DATABASE_URL", "").strip()
        or os.getenv("SUPABASE_DATABASE_URL", "").strip()
    )
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL yoki SUPABASE_DATABASE_URL kerak (Supabase PostgreSQL), "
            "yoki SUPABASE_DB_HOST + SUPABASE_DB_PASSWORD"
        )
    if len(dsn) >= 2 and dsn[0] == dsn[-1] and dsn[0] in "\"'":
        dsn = dsn[1:-1].strip()
    if dsn.startswith("http://") or dsn.startswith("https://"):
        raise RuntimeError(
            "DATABASE_URL noto'g'ri: https:// emas, postgresql://... kerak — "
            "Supabase → Database → Connection string."
        )
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]
    if not dsn.startswith("postgresql://"):
        raise RuntimeError("DATABASE_URL postgresql://... bo'lishi kerak")
    parsed = urlparse(dsn)
    if not parsed.hostname:
        raise RuntimeError(
            "DATABASE_URL noto'g'ri: host ko'rinmayapti. "
            "Parolda maxsus belgilar bo'lsa SUPABASE_DB_HOST + SUPABASE_DB_PASSWORD ishlatíng."
        )
    return dsn


def _pg_password_explicit() -> str | None:
    return (
        os.getenv("SUPABASE_DB_PASSWORD", "").strip()
        or os.getenv("PGPASSWORD", "").strip()
        or None
    )


def _use_explicit_pg_params() -> bool:
    return bool(os.getenv("SUPABASE_DB_HOST", "").strip()) and bool(_pg_password_explicit())


async def _create_pool() -> asyncpg.Pool:
    common = {"min_size": 1, "max_size": 10, "statement_cache_size": 0}
    if _use_explicit_pg_params():
        host = os.getenv("SUPABASE_DB_HOST", "").strip()
        password = _pg_password_explicit()
        assert password is not None
        try:
            return await asyncpg.create_pool(
                host=host,
                port=int(os.getenv("SUPABASE_DB_PORT", "5432")),
                user=os.getenv("SUPABASE_DB_USER", "postgres").strip(),
                password=password,
                database=os.getenv("SUPABASE_DB_NAME", "postgres").strip(),
                ssl=True,
                **common,
            )
        except socket.gaierror as exc:
            raise RuntimeError(
                f"DNS: host {host!r} topilmadi. Supabase → Database → Host ni tekshiring."
            ) from exc

    dsn = _dsn()
    parsed = urlparse(dsn)
    kwargs: dict[str, Any] = dict(common)
    host = (parsed.hostname or "").lower()
    if "supabase" in host or parsed.port == 6543:
        kwargs["ssl"] = True
    try:
        return await asyncpg.create_pool(dsn, **kwargs)
    except socket.gaierror as exc:
        raise RuntimeError(
            f"DNS: URI dagi host {parsed.hostname!r} topilmadi. "
            "Internet/VPN yoki Supabase URI ni tekshiring."
        ) from exc


async def close_pool() -> None:
    global _pool, _default_category_id_cache
    if _pool is not None:
        await _pool.close()
        _pool = None
    _default_category_id_cache = None


def is_ready() -> bool:
    return _pool is not None


async def init_db() -> None:
    global _pool
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _pool is None:
        _pool = await _create_pool()
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_group_categories (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id BIGSERIAL PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                logo_path TEXT
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_groups (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                category_id BIGINT REFERENCES promo_group_categories(id) ON DELETE RESTRICT
            );
            """
        )
        await conn.execute(
            "ALTER TABLE promo_groups ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0;"
        )
        await conn.execute(
            """
            ALTER TABLE promo_groups
            ADD COLUMN IF NOT EXISTS category_id BIGINT
            REFERENCES promo_group_categories(id) ON DELETE RESTRICT;
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promos (
                id BIGSERIAL PRIMARY KEY,
                code TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                group_id BIGINT NOT NULL REFERENCES promo_groups(id) ON DELETE RESTRICT,
                UNIQUE (group_id, code)
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS track_entries (
                id BIGSERIAL PRIMARY KEY,
                link_id BIGINT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
                promo_id BIGINT NOT NULL REFERENCES promos(id) ON DELETE CASCADE,
                token TEXT NOT NULL UNIQUE,
                clicks BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL,
                UNIQUE (link_id, promo_id)
            );
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_track_token ON track_entries(token);"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS track_visitors (
                id BIGSERIAL PRIMARY KEY,
                token TEXT NOT NULL REFERENCES track_entries(token) ON DELETE CASCADE,
                visitor_id TEXT NOT NULL,
                ip_ua_hash TEXT,
                first_seen TIMESTAMPTZ NOT NULL,
                UNIQUE (token, visitor_id)
            );
            """
        )
        await conn.execute(
            "ALTER TABLE track_visitors ADD COLUMN IF NOT EXISTS ip_ua_hash TEXT;"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_track_visitors_token ON track_visitors(token);"
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_track_visitors_ip_ua
            ON track_visitors(token, ip_ua_hash, first_seen);
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_track_visitors_first_seen ON track_visitors(first_seen);"
        )
        await conn.execute(
            "ALTER TABLE links ADD COLUMN IF NOT EXISTS logo_path TEXT;"
        )

        # Default kategoriya + Umumiy guruh + category_id backfill
        await conn.execute(
            """
            INSERT INTO promo_group_categories (name, created_at, priority)
            VALUES ($1, $2, 1)
            ON CONFLICT (name) DO NOTHING;
            """,
            DEFAULT_CATEGORY_NAME,
            _now_utc(),
        )
        category_id = await conn.fetchval(
            "SELECT id FROM promo_group_categories WHERE name = $1",
            DEFAULT_CATEGORY_NAME,
        )
        await conn.execute(
            """
            UPDATE promo_groups
            SET category_id = $1
            WHERE category_id IS NULL;
            """,
            category_id,
        )
        await conn.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'promo_groups' AND column_name = 'category_id'
                      AND is_nullable = 'YES'
                ) THEN
                    ALTER TABLE promo_groups ALTER COLUMN category_id SET NOT NULL;
                END IF;
            EXCEPTION WHEN others THEN
                NULL;
            END $$;
            """
        )
        await conn.execute(
            """
            INSERT INTO promo_groups (name, created_at, priority, category_id)
            VALUES (
                'Umumiy',
                $1,
                COALESCE((SELECT MAX(priority) FROM promo_groups), 0) + 1,
                $2
            )
            ON CONFLICT (name) DO NOTHING;
            """,
            _now_utc(),
            category_id,
        )
    await sync_category_priorities()


async def sync_category_priorities(conn: asyncpg.Connection | None = None) -> int:
    own = conn is None
    if own:
        assert _pool is not None
        conn = await _pool.acquire()
    assert conn is not None
    try:
        rows = await conn.fetch("SELECT id, name, priority FROM promo_group_categories")
        if not rows:
            return 0
        zeros = [row for row in rows if int(row["priority"] or 0) == 0]
        nonzeros = [row for row in rows if int(row["priority"] or 0) != 0]
        updates: list[tuple[int, int]] = []
        if not nonzeros:
            unmatched: list[asyncpg.Record] = []
            for row in rows:
                priority = _category_priority_for_name(str(row["name"]))
                if priority is None:
                    unmatched.append(row)
                else:
                    updates.append((priority, int(row["id"])))
            base = len(CATEGORY_PRIORITY_ORDER) + 1
            for index, row in enumerate(
                sorted(unmatched, key=lambda item: str(item["name"]).casefold())
            ):
                updates.append((base + index, int(row["id"])))
        elif zeros:
            base = max(int(row["priority"]) for row in nonzeros) + 1
            for index, row in enumerate(
                sorted(zeros, key=lambda item: str(item["name"]).casefold())
            ):
                updates.append((base + index, int(row["id"])))
        for priority, category_id in updates:
            await conn.execute(
                "UPDATE promo_group_categories SET priority = $1 WHERE id = $2",
                priority,
                category_id,
            )
        return len(updates)
    finally:
        if own:
            await _pool.release(conn)


async def move_category_priority(category_id: int, *, direction: int) -> bool:
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    assert _pool is not None
    async with _pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id FROM promo_group_categories
                ORDER BY priority ASC, LOWER(name), id
                FOR UPDATE
                """
            )
            ids = [int(row["id"]) for row in rows]
            if category_id not in ids:
                return False
            index = ids.index(category_id)
            target = index + direction
            if target < 0 or target >= len(ids):
                return False
            ids[index], ids[target] = ids[target], ids[index]
            await conn.executemany(
                "UPDATE promo_group_categories SET priority = $1 WHERE id = $2",
                [(priority, cid) for priority, cid in enumerate(ids, start=1)],
            )
            return True


def disk_path_for_link_logo(link_id: int) -> Path:
    return DATA_DIR / "logos" / f"link_{link_id}.png"


async def add_link(url: str, title: str | None = None) -> int:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO links (url, title, created_at, logo_path)
            VALUES ($1, $2, $3, NULL)
            RETURNING id
            """,
            url.strip(),
            (title or "").strip() or None,
            _now_utc(),
        )
        assert row is not None
        return int(row["id"])


async def get_link(link_id: int) -> dict[str, Any] | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, url, title, created_at, logo_path FROM links WHERE id = $1",
            link_id,
        )
        return _row(row) if row else None


async def set_link_logo_path(link_id: int, path: str | None) -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE links SET logo_path = $1 WHERE id = $2", path, link_id
        )


async def update_link_fields(link_id: int, *, url: str | None = None, title: Any = _UNSET) -> bool:
    if not await get_link(link_id):
        return False
    fields: list[str] = []
    values: list[Any] = []
    idx = 1
    if url is not None:
        fields.append(f"url = ${idx}")
        values.append(url.strip())
        idx += 1
    if title is not _UNSET:
        fields.append(f"title = ${idx}")
        values.append(str(title).strip() or None if title is not None else None)
        idx += 1
    if not fields:
        return True
    values.append(link_id)
    assert _pool is not None
    async with _pool.acquire() as conn:
        status = await conn.execute(
            f"UPDATE links SET {', '.join(fields)} WHERE id = ${idx}",
            *values,
        )
        return not status.endswith("0")


async def delete_link(link_id: int) -> bool:
    row = await get_link(link_id)
    if not row:
        return False
    logo_path = (row.get("logo_path") or "").strip()
    if logo_path:
        Path(logo_path).unlink(missing_ok=True)
    disk_path_for_link_logo(link_id).unlink(missing_ok=True)
    assert _pool is not None
    async with _pool.acquire() as conn:
        status = await conn.execute("DELETE FROM links WHERE id = $1", link_id)
        return not status.endswith("0")


async def ensure_default_category() -> int:
    global _default_category_id_cache
    if _default_category_id_cache is None:
        _default_category_id_cache = await ensure_category(DEFAULT_CATEGORY_NAME)
    return _default_category_id_cache


async def add_category(name: str) -> int:
    cleaned = name.strip()
    assert _pool is not None
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO promo_group_categories (name, created_at, priority)
                VALUES (
                    $1, $2,
                    COALESCE((SELECT MAX(priority) FROM promo_group_categories), 0) + 1
                )
                RETURNING id
                """,
                cleaned,
                _now_utc(),
            )
            assert row is not None
            return int(row["id"])
    except UniqueViolationError as exc:
        raise DuplicateError("duplicate category name") from exc


async def ensure_category(name: str) -> int:
    cleaned = name.strip()
    assert _pool is not None
    async with _pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT id FROM promo_group_categories WHERE name = $1", cleaned
        )
    if value is not None:
        return int(value)
    try:
        return await add_category(cleaned)
    except DuplicateError:
        async with _pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT id FROM promo_group_categories WHERE name = $1", cleaned
            )
        if value is None:
            raise
        return int(value)


async def list_categories() -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, created_at, priority
            FROM promo_group_categories
            ORDER BY priority ASC, LOWER(name), id
            """
        )
        return [_row(row) for row in rows]


async def get_category(category_id: int) -> dict[str, Any] | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, created_at, priority FROM promo_group_categories WHERE id = $1",
            category_id,
        )
        return _row(row) if row else None


async def update_category(category_id: int, *, name: Any = _UNSET) -> bool:
    if not await get_category(category_id):
        return False
    if name is _UNSET:
        return True
    cleaned = str(name).strip()
    if len(cleaned) < 2:
        return False
    assert _pool is not None
    try:
        async with _pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE promo_group_categories SET name = $1 WHERE id = $2",
                cleaned,
                category_id,
            )
            return not status.endswith("0")
    except UniqueViolationError as exc:
        raise DuplicateError("duplicate category name") from exc


async def add_group(name: str, category_id: int | None = None) -> int:
    cleaned = name.strip()
    category_id = category_id if category_id is not None else await ensure_default_category()
    assert _pool is not None
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO promo_groups (name, created_at, priority, category_id)
                VALUES (
                    $1, $2,
                    COALESCE((SELECT MAX(priority) FROM promo_groups), 0) + 1,
                    $3
                )
                RETURNING id
                """,
                cleaned,
                _now_utc(),
                category_id,
            )
            assert row is not None
            return int(row["id"])
    except UniqueViolationError as exc:
        raise DuplicateError("duplicate group name") from exc


async def ensure_group(name: str, category_id: int | None = None) -> int:
    cleaned = name.strip()
    category_id = category_id if category_id is not None else await ensure_default_category()
    assert _pool is not None
    async with _pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT id FROM promo_groups WHERE name = $1 AND category_id = $2",
            cleaned,
            category_id,
        )
    return int(value) if value is not None else await add_group(cleaned, category_id)


async def list_groups(category_id: int | None = None) -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        if category_id is not None:
            rows = await conn.fetch(
                """
                SELECT id, name, created_at, priority, category_id
                FROM promo_groups
                WHERE category_id = $1
                ORDER BY priority ASC, LOWER(name), id
                """,
                category_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, name, created_at, priority, category_id
                FROM promo_groups
                ORDER BY priority ASC, LOWER(name), id
                """
            )
        return [_row(row) for row in rows]


async def get_group(group_id: int) -> dict[str, Any] | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, created_at, priority, category_id
            FROM promo_groups WHERE id = $1
            """,
            group_id,
        )
        return _row(row) if row else None


async def get_group_by_name(name: str) -> dict[str, Any] | None:
    cleaned = name.strip()
    if not cleaned:
        return None
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, created_at, priority, category_id
            FROM promo_groups WHERE name = $1
            """,
            cleaned,
        )
        return _row(row) if row else None


async def update_group_category(group_id: int, category_id: int) -> bool:
    return await update_group_fields(group_id, category_id=category_id)


async def update_group_fields(
    group_id: int, *, name: Any = _UNSET, category_id: Any = _UNSET
) -> bool:
    if not await get_group(group_id):
        return False
    fields: list[str] = []
    values: list[Any] = []
    idx = 1
    if name is not _UNSET:
        cleaned = str(name).strip()
        if len(cleaned) < 2:
            return False
        fields.append(f"name = ${idx}")
        values.append(cleaned)
        idx += 1
    if category_id is not _UNSET:
        if not isinstance(category_id, int) or not await get_category(category_id):
            return False
        fields.append(f"category_id = ${idx}")
        values.append(category_id)
        idx += 1
    if not fields:
        return True
    values.append(group_id)
    assert _pool is not None
    try:
        async with _pool.acquire() as conn:
            status = await conn.execute(
                f"UPDATE promo_groups SET {', '.join(fields)} WHERE id = ${idx}",
                *values,
            )
            return not status.endswith("0")
    except UniqueViolationError as exc:
        raise DuplicateError("duplicate group name") from exc


async def add_promo(code: str, group_id: int) -> int:
    code = code.strip()
    if not await get_group(group_id):
        raise ValueError("group not found")
    assert _pool is not None
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO promos (code, created_at, group_id)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                code,
                _now_utc(),
                group_id,
            )
            assert row is not None
            return int(row["id"])
    except UniqueViolationError as exc:
        raise DuplicateError("duplicate promo code") from exc


async def list_links() -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, url, title, created_at, logo_path FROM links ORDER BY id DESC"
        )
        return [_row(row) for row in rows]


_PROMO_SELECT = """
    SELECT p.id, p.code, p.created_at, p.group_id, g.name AS group_name,
           g.category_id, c.name AS category_name
    FROM promos p
    JOIN promo_groups g ON g.id = p.group_id
    JOIN promo_group_categories c ON c.id = g.category_id
"""


async def list_promos() -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            _PROMO_SELECT
            + " ORDER BY c.priority ASC, LOWER(c.name), g.priority ASC, LOWER(g.name), LOWER(p.code), p.id"
        )
        return [_row(row) for row in rows]


async def list_promos_by_group(group_id: int) -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            _PROMO_SELECT + " WHERE p.group_id = $1 ORDER BY LOWER(p.code), p.id DESC",
            group_id,
        )
        return [_row(row) for row in rows]


async def list_promos_by_category(category_id: int) -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            _PROMO_SELECT
            + " WHERE g.category_id = $1 ORDER BY g.priority ASC, LOWER(g.name), LOWER(p.code), p.id",
            category_id,
        )
        return [_row(row) for row in rows]


async def get_promo_code_by_id(promo_id: int) -> str | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        value = await conn.fetchval("SELECT code FROM promos WHERE id = $1", promo_id)
        return str(value) if value is not None else None


async def get_promo(promo_id: int) -> dict[str, Any] | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.id, p.code, p.created_at, p.group_id, g.name AS group_name
            FROM promos p
            JOIN promo_groups g ON g.id = p.group_id
            WHERE p.id = $1
            """,
            promo_id,
        )
        return _row(row) if row else None


async def update_promo_fields(
    promo_id: int, *, code: Any = _UNSET, group_id: Any = _UNSET
) -> bool:
    if not await get_promo(promo_id):
        return False
    fields: list[str] = []
    values: list[Any] = []
    idx = 1
    if code is not _UNSET:
        fields.append(f"code = ${idx}")
        values.append(str(code).strip())
        idx += 1
    if group_id is not _UNSET:
        if not isinstance(group_id, int) or not await get_group(group_id):
            return False
        fields.append(f"group_id = ${idx}")
        values.append(group_id)
        idx += 1
    if not fields:
        return True
    values.append(promo_id)
    assert _pool is not None
    try:
        async with _pool.acquire() as conn:
            status = await conn.execute(
                f"UPDATE promos SET {', '.join(fields)} WHERE id = ${idx}",
                *values,
            )
            return not status.endswith("0")
    except UniqueViolationError as exc:
        raise DuplicateError("duplicate promo code") from exc


async def get_track_token(link_id: int, promo_id: int) -> str | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT token FROM track_entries WHERE link_id = $1 AND promo_id = $2",
            link_id,
            promo_id,
        )
        return str(value) if value is not None else None


async def create_track_entry(link_id: int, promo_id: int) -> str:
    existing = await get_track_token(link_id, promo_id)
    if existing:
        return existing
    assert _pool is not None
    for _ in range(4):
        token = secrets.token_urlsafe(16).rstrip("=").replace("-", "_")
        try:
            async with _pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO track_entries (link_id, promo_id, token, clicks, created_at)
                    VALUES ($1, $2, $3, 0, $4)
                    """,
                    link_id,
                    promo_id,
                    token,
                    _now_utc(),
                )
            return token
        except UniqueViolationError:
            existing = await get_track_token(link_id, promo_id)
            if existing:
                return existing
    raise RuntimeError("track_entries uchun token yaratib bo'lmadi")


async def ensure_track_tokens_for_promos(
    link_id: int, promo_ids: list[int]
) -> dict[int, str]:
    if not promo_ids:
        return {}
    unique_ids = list(dict.fromkeys(promo_ids))
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT promo_id, token FROM track_entries
            WHERE link_id = $1 AND promo_id = ANY($2::bigint[])
            """,
            link_id,
            unique_ids,
        )
        result = {int(row["promo_id"]): str(row["token"]) for row in rows}
        missing = [promo_id for promo_id in unique_ids if promo_id not in result]
        if not missing:
            return result
        now = _now_utc()
        batch = [
            (
                link_id,
                promo_id,
                secrets.token_urlsafe(16).rstrip("=").replace("-", "_"),
                now,
            )
            for promo_id in missing
        ]
        try:
            await conn.executemany(
                """
                INSERT INTO track_entries (link_id, promo_id, token, clicks, created_at)
                VALUES ($1, $2, $3, 0, $4)
                ON CONFLICT (link_id, promo_id) DO NOTHING
                """,
                batch,
            )
        except UniqueViolationError:
            for link_id_, promo_id, tok, ts in batch:
                try:
                    await conn.execute(
                        """
                        INSERT INTO track_entries (link_id, promo_id, token, clicks, created_at)
                        VALUES ($1, $2, $3, 0, $4)
                        ON CONFLICT (link_id, promo_id) DO NOTHING
                        """,
                        link_id_,
                        promo_id,
                        tok,
                        ts,
                    )
                except UniqueViolationError:
                    pass
        rows = await conn.fetch(
            """
            SELECT promo_id, token FROM track_entries
            WHERE link_id = $1 AND promo_id = ANY($2::bigint[])
            """,
            link_id,
            unique_ids,
        )
        return {int(row["promo_id"]): str(row["token"]) for row in rows}


async def get_link_url_by_token(token: str) -> str | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT l.url
            FROM track_entries t
            JOIN links l ON l.id = t.link_id
            WHERE t.token = $1
            """,
            token,
        )
        return str(value) if value is not None else None


async def record_visit(
    token: str,
    visitor_id: str,
    ip_ua_hash: str | None = None,
    dedup_window_hours: float = 24.0,
) -> bool:
    assert _pool is not None
    async with _pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT id FROM track_visitors
                WHERE token = $1
                  AND (
                    visitor_id = $2
                    OR (
                        $3::text IS NOT NULL
                        AND ip_ua_hash = $3
                        AND first_seen > NOW() - ($4 * INTERVAL '1 hour')
                    )
                  )
                LIMIT 1
                """,
                token,
                visitor_id,
                ip_ua_hash,
                dedup_window_hours,
            )
            if existing is not None:
                return False
            status = await conn.execute(
                """
                INSERT INTO track_visitors (token, visitor_id, ip_ua_hash, first_seen)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (token, visitor_id) DO NOTHING
                """,
                token,
                visitor_id,
                ip_ua_hash,
                _now_utc(),
            )
            if status.endswith("0"):
                return False
            await conn.execute(
                "UPDATE track_entries SET clicks = clicks + 1 WHERE token = $1",
                token,
            )
            return True


async def cleanup_old_visitors(days: int = 90, *, batch_size: int = 5000) -> int:
    if days < 1:
        return 0
    batch_size = max(batch_size, 1)
    assert _pool is not None
    total = 0
    async with _pool.acquire() as conn:
        while True:
            rows = await conn.fetch(
                """
                DELETE FROM track_visitors
                WHERE id IN (
                    SELECT id FROM track_visitors
                    WHERE first_seen < NOW() - ($1 * INTERVAL '1 day')
                    LIMIT $2
                )
                RETURNING id
                """,
                days,
                batch_size,
            )
            count = len(rows)
            total += count
            if count < batch_size:
                return total


async def stats_for_group(group_id: int) -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id AS promo_id, p.code AS promo_code, COALESCE(c.clicks, 0) AS clicks
            FROM promos p
            LEFT JOIN (
                SELECT promo_id, SUM(clicks) AS clicks FROM track_entries GROUP BY promo_id
            ) c ON c.promo_id = p.id
            WHERE p.group_id = $1
            ORDER BY COALESCE(c.clicks, 0) DESC, LOWER(p.code)
            """,
            group_id,
        )
        return [{**_row(row), "clicks": int(row["clicks"])} for row in rows]


async def stats_summary_by_group() -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT g.id AS group_id, g.name AS group_name, g.priority, g.category_id,
                   c.name AS category_name, p.code AS promo_code, COALESCE(tc.clicks, 0) AS clicks
            FROM promos p
            JOIN promo_groups g ON g.id = p.group_id
            JOIN promo_group_categories c ON c.id = g.category_id
            LEFT JOIN (
                SELECT promo_id, SUM(clicks) AS clicks FROM track_entries GROUP BY promo_id
            ) tc ON tc.promo_id = p.id
            ORDER BY c.priority ASC, LOWER(c.name), g.priority ASC, LOWER(g.name), LOWER(p.code)
            """
        )
        return [{**_row(row), "clicks": int(row["clicks"])} for row in rows]


async def stats_group_totals_desc() -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT g.id AS group_id, g.name AS group_name, g.priority, g.category_id,
                   c.name AS category_name,
                   COALESCE(s.promo_count, 0) AS promo_count,
                   COALESCE(s.clicks, 0) AS clicks
            FROM promo_groups g
            JOIN promo_group_categories c ON c.id = g.category_id
            LEFT JOIN (
                SELECT p.group_id, COUNT(*) AS promo_count, COALESCE(SUM(tc.clicks), 0) AS clicks
                FROM promos p
                LEFT JOIN (
                    SELECT promo_id, SUM(clicks) AS clicks FROM track_entries GROUP BY promo_id
                ) tc ON tc.promo_id = p.id
                GROUP BY p.group_id
            ) s ON s.group_id = g.id
            ORDER BY c.priority ASC, LOWER(c.name), g.priority ASC, LOWER(g.name)
            """
        )
        return [
            {
                **_row(row),
                "clicks": int(row["clicks"]),
                "promo_count": int(row["promo_count"]),
            }
            for row in rows
        ]


async def stats_category_totals() -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id AS category_id, c.name AS category_name, c.priority,
                   COALESCE(s.group_count, 0) AS group_count,
                   COALESCE(s.promo_count, 0) AS promo_count,
                   COALESCE(s.clicks, 0) AS clicks
            FROM promo_group_categories c
            LEFT JOIN (
                SELECT g.category_id,
                       COUNT(DISTINCT g.id) AS group_count,
                       COUNT(p.id) AS promo_count,
                       COALESCE(SUM(tc.clicks), 0) AS clicks
                FROM promo_groups g
                LEFT JOIN promos p ON p.group_id = g.id
                LEFT JOIN (
                    SELECT promo_id, SUM(clicks) AS clicks FROM track_entries GROUP BY promo_id
                ) tc ON tc.promo_id = p.id
                GROUP BY g.category_id
            ) s ON s.category_id = c.id
            ORDER BY c.priority ASC, LOWER(c.name), c.id
            """
        )
        return [
            {
                **_row(row),
                "clicks": int(row["clicks"]),
                "group_count": int(row["group_count"]),
                "promo_count": int(row["promo_count"]),
            }
            for row in rows
        ]
