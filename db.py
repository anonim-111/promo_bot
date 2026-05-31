import os
import secrets
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncpg
from asyncpg.exceptions import UniqueViolationError

DATA_DIR = Path(__file__).resolve().parent / "data"
# Eski kod bilan mos: logo yo‘li `data/logos/` (promo.db fayli endi ishlatilmaydi).
DB_PATH = DATA_DIR / "promo.db"

_pool: asyncpg.Pool | None = None


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
    # .env da qo'shtirnoq bilan yozilsa
    if len(dsn) >= 2 and dsn[0] == dsn[-1] and dsn[0] in "\"'":
        dsn = dsn[1:-1].strip()
    if dsn.startswith("http://") or dsn.startswith("https://"):
        raise RuntimeError(
            "DATABASE_URL noto'g'ri: https:// (Next.js API URL) emas, "
            "postgresql://... kerak — Supabase → Database → Connection string."
        )
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]
    parsed = urlparse(dsn)
    if not parsed.hostname:
        raise RuntimeError(
            "DATABASE_URL noto'g'ri: host (masalan db.xxxxx.supabase.co) ko'rinmayapti. "
            "Parolda @ # : $ kabi belgilar bo'lsa, SUPABASE_DB_HOST + SUPABASE_DB_PASSWORD "
            "islang yoki parolni URL-encode qiling."
        )
    return dsn


def _pg_password_explicit() -> str | None:
    return (
        os.getenv("SUPABASE_DB_PASSWORD", "").strip()
        or os.getenv("PGPASSWORD", "").strip()
        or None
    )


def _use_explicit_pg_params() -> bool:
    """URI parse muammosiz: maxsus belgili parollar uchun."""
    return bool(os.getenv("SUPABASE_DB_HOST", "").strip()) and bool(_pg_password_explicit())


async def _create_pool() -> asyncpg.Pool:
    if _use_explicit_pg_params():
        host = os.getenv("SUPABASE_DB_HOST", "").strip()
        password = _pg_password_explicit()
        assert password is not None
        user = os.getenv("SUPABASE_DB_USER", "postgres").strip()
        port = int(os.getenv("SUPABASE_DB_PORT", "5432"))
        database = os.getenv("SUPABASE_DB_NAME", "postgres").strip()
        try:
            return await asyncpg.create_pool(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                min_size=1,
                max_size=10,
                statement_cache_size=0,
                ssl=True,
            )
        except socket.gaierror as e:
            raise RuntimeError(
                f"DNS: host {host!r} topilmadi. Supabase → Database → "
                f"Host ni tekshiring (odatda db.xxxxx.supabase.co, port 5432 yoki pooler 6543)."
            ) from e
    dsn = _dsn()
    parsed = urlparse(dsn)
    try:
        return await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=10,
            statement_cache_size=0,
        )
    except socket.gaierror as e:
        raise RuntimeError(
            f"DNS: URI dagi host {parsed.hostname!r} topilmadi. "
            "A) Internet/VPN; B) Supabase URI ni qayta nusxalang; "
            "C) Parolda $ & bo'lsa .env da SUPABASE_DB_HOST + SUPABASE_DB_PASSWORD ishlating."
        ) from e


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _row(r: asyncpg.Record) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for k in r.keys():
        v = r[k]
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


async def init_db() -> None:
    global _pool
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _pool is None:
        _pool = await _create_pool()
    assert _pool is not None
    async with _pool.acquire() as conn:
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
                priority INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promos (
                id BIGSERIAL PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL,
                group_id BIGINT REFERENCES promo_groups(id) ON DELETE RESTRICT
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
                UNIQUE(link_id, promo_id)
            );
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_track_token ON track_entries(token);"
        )
    await _migrate_schema()


async def _migrate_schema() -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE links ADD COLUMN IF NOT EXISTS logo_path TEXT;"
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_groups (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await conn.execute(
            "ALTER TABLE promo_groups ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0;"
        )
        await conn.execute(
            """
            UPDATE promo_groups g
            SET priority = sub.rn
            FROM (
                SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn
                FROM promo_groups
            ) sub
            WHERE g.id = sub.id AND g.priority = 0;
            """
        )
        await conn.execute(
            "ALTER TABLE promos ADD COLUMN IF NOT EXISTS group_id BIGINT;"
        )
        await conn.execute(
            """
            DO $$
            DECLARE
                gid BIGINT;
            BEGIN
                INSERT INTO promo_groups (name, created_at)
                VALUES ('Umumiy', NOW())
                ON CONFLICT (name) DO NOTHING;

                SELECT id INTO gid FROM promo_groups WHERE name = 'Umumiy' LIMIT 1;
                UPDATE promos SET group_id = gid WHERE group_id IS NULL;
            END $$;
            """
        )
        await conn.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'promos_group_id_fkey'
                ) THEN
                    ALTER TABLE promos
                    ADD CONSTRAINT promos_group_id_fkey
                    FOREIGN KEY (group_id) REFERENCES promo_groups(id) ON DELETE RESTRICT;
                END IF;
            END $$;
            """
        )
        await conn.execute(
            "ALTER TABLE promos ALTER COLUMN group_id SET NOT NULL;"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_promos_group_id ON promos(group_id);"
        )


def disk_path_for_link_logo(link_id: int) -> Path:
    """QR markazidagi logo fayli (PNG)."""
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
            """
            SELECT id, url, title, created_at, logo_path
            FROM links WHERE id = $1
            """,
            link_id,
        )
        return _row(row) if row else None


async def set_link_logo_path(link_id: int, path: str | None) -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE links SET logo_path = $1 WHERE id = $2",
            path,
            link_id,
        )


_UNSET = object()


async def update_link_fields(
    link_id: int,
    *,
    url: str | None = None,
    title: Any = _UNSET,
) -> bool:
    """
    Faqat berilgan maydonlarni yangilaydi.
    title=... berilsa (bo'sh qator ham) sarlavha yangilanadi; title o'tkazilmasa — o'zgarmaydi.
    """
    row = await get_link(link_id)
    if not row:
        return False
    parts: list[str] = []
    vals: list[object] = []
    n = 1
    if url is not None:
        parts.append(f"url = ${n}")
        vals.append(url.strip())
        n += 1
    if title is not _UNSET:
        parts.append(f"title = ${n}")
        vals.append((str(title).strip() or None) if title is not None else None)
        n += 1
    if not parts:
        return True
    vals.append(link_id)
    sql = f"UPDATE links SET {', '.join(parts)} WHERE id = ${n}"
    assert _pool is not None
    async with _pool.acquire() as conn:
        status = await conn.execute(sql, *vals)
        try:
            return int(status.split()[-1]) > 0
        except (ValueError, IndexError):
            return True


async def delete_link(link_id: int) -> bool:
    """Link, tracking yozuvlari (CASCADE) va logo fayllarini o'chiradi."""
    row = await get_link(link_id)
    if not row:
        return False
    lp = (row.get("logo_path") or "").strip()
    if lp:
        Path(lp).unlink(missing_ok=True)
    disk_path_for_link_logo(link_id).unlink(missing_ok=True)
    assert _pool is not None
    async with _pool.acquire() as conn:
        result = await conn.execute("DELETE FROM links WHERE id = $1", link_id)
        try:
            return int(result.split()[-1]) > 0
        except (ValueError, IndexError):
            return False


async def add_group(name: str) -> int:
    cleaned = name.strip()
    assert _pool is not None
    try:
        async with _pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO promo_groups (name, created_at, priority)
                VALUES (
                    $1, $2,
                    COALESCE((SELECT MAX(priority) FROM promo_groups), 0) + 1
                )
                RETURNING id
                """,
                cleaned,
                _now_utc(),
            )
            assert row is not None
            return int(row["id"])
    except UniqueViolationError as e:
        raise sqlite3.IntegrityError("duplicate group name") from e


async def ensure_group(name: str) -> int:
    """Guruh bor bo'lsa id ni qaytaradi, yo'q bo'lsa yaratadi."""
    cleaned = name.strip()
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM promo_groups WHERE name = $1",
            cleaned,
        )
        if row is not None:
            return int(row["id"])
    return await add_group(cleaned)


async def list_groups() -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, created_at, priority
            FROM promo_groups
            ORDER BY priority ASC, LOWER(name), id
            """
        )
        return [_row(r) for r in rows]


async def get_group(group_id: int) -> dict[str, Any] | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, created_at, priority FROM promo_groups WHERE id = $1",
            group_id,
        )
        return _row(row) if row else None


async def add_promo(code: str, group_id: int) -> int:
    code = code.strip()
    assert _pool is not None
    if not await get_group(group_id):
        raise ValueError("group not found")
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
    except UniqueViolationError as e:
        raise sqlite3.IntegrityError("duplicate promo code") from e


async def list_links() -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, url, title, created_at, logo_path
            FROM links ORDER BY id DESC
            """
        )
        return [_row(r) for r in rows]


async def list_promos() -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.code, p.created_at, p.group_id, g.name AS group_name
            FROM promos p
            JOIN promo_groups g ON g.id = p.group_id
            ORDER BY p.id DESC
            """
        )
        return [_row(r) for r in rows]


async def list_promos_by_group(group_id: int) -> list[dict[str, Any]]:
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.code, p.created_at, p.group_id, g.name AS group_name
            FROM promos p
            JOIN promo_groups g ON g.id = p.group_id
            WHERE p.group_id = $1
            ORDER BY LOWER(p.code), p.id DESC
            """,
            group_id,
        )
        return [_row(r) for r in rows]


async def get_promo_code_by_id(promo_id: int) -> str | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT code FROM promos WHERE id = $1",
            promo_id,
        )
        return str(row["code"]) if row else None


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
    promo_id: int,
    *,
    code: Any = _UNSET,
    group_id: Any = _UNSET,
) -> bool:
    row = await get_promo(promo_id)
    if not row:
        return False
    parts: list[str] = []
    vals: list[object] = []
    n = 1
    if code is not _UNSET:
        parts.append(f"code = ${n}")
        vals.append(str(code).strip())
        n += 1
    if group_id is not _UNSET:
        if not isinstance(group_id, int) or not await get_group(group_id):
            return False
        parts.append(f"group_id = ${n}")
        vals.append(group_id)
        n += 1
    if not parts:
        return True
    vals.append(promo_id)
    sql = f"UPDATE promos SET {', '.join(parts)} WHERE id = ${n}"
    assert _pool is not None
    try:
        async with _pool.acquire() as conn:
            status = await conn.execute(sql, *vals)
            try:
                return int(status.split()[-1]) > 0
            except (ValueError, IndexError):
                return True
    except UniqueViolationError as e:
        raise sqlite3.IntegrityError("duplicate promo code") from e


async def get_track_token(link_id: int, promo_id: int) -> str | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT token FROM track_entries
            WHERE link_id = $1 AND promo_id = $2
            """,
            link_id,
            promo_id,
        )
        return str(row["token"]) if row else None


async def create_track_entry(link_id: int, promo_id: int) -> str:
    """Returns unique tracking token (existing pair returns same token)."""
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
            again = await get_track_token(link_id, promo_id)
            if again:
                return again
            continue
    raise RuntimeError("track_entries uchun token yaratib bo'lmadi")


async def ensure_track_tokens_for_promos(
    link_id: int, promo_ids: list[int]
) -> dict[int, str]:
    """
    Bir nechta promo uchun tokenlarni bir ulanishda tayyorlaydi (Excel / mass export uchun).
    Mavjud juftliklar o'zgarmaydi.
    """
    if not promo_ids:
        return {}
    unique_ids = list(dict.fromkeys(promo_ids))
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT promo_id, token
            FROM track_entries
            WHERE link_id = $1 AND promo_id = ANY($2::bigint[])
            """,
            link_id,
            unique_ids,
        )
        out: dict[int, str] = {int(r["promo_id"]): str(r["token"]) for r in rows}
        missing = [pid for pid in unique_ids if pid not in out]
        if not missing:
            return out
        now = _now_utc()
        batch: list[tuple[int, str, datetime]] = []
        for pid in missing:
            token = secrets.token_urlsafe(16).rstrip("=").replace("-", "_")
            batch.append((pid, token, now))
        try:
            await conn.executemany(
                """
                INSERT INTO track_entries (link_id, promo_id, token, clicks, created_at)
                VALUES ($1, $2, $3, 0, $4)
                """,
                [(link_id, pid, tok, ts) for pid, tok, ts in batch],
            )
        except UniqueViolationError:
            for pid, tok, ts in batch:
                try:
                    await conn.execute(
                        """
                        INSERT INTO track_entries (link_id, promo_id, token, clicks, created_at)
                        VALUES ($1, $2, $3, 0, $4)
                        """,
                        link_id,
                        pid,
                        tok,
                        ts,
                    )
                except UniqueViolationError:
                    row = await conn.fetchrow(
                        """
                        SELECT token FROM track_entries
                        WHERE link_id = $1 AND promo_id = $2
                        """,
                        link_id,
                        pid,
                    )
                    if row:
                        out[pid] = str(row["token"])
        rows2 = await conn.fetch(
            """
            SELECT promo_id, token
            FROM track_entries
            WHERE link_id = $1 AND promo_id = ANY($2::bigint[])
            """,
            link_id,
            unique_ids,
        )
        return {int(r["promo_id"]): str(r["token"]) for r in rows2}


async def get_link_url_by_token(token: str) -> str | None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT l.url AS url
            FROM track_entries t
            JOIN links l ON l.id = t.link_id
            WHERE t.token = $1
            """,
            token,
        )
        return str(row["url"]) if row else None


async def increment_click(token: str) -> None:
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE track_entries SET clicks = clicks + 1 WHERE token = $1
            """,
            token,
        )


async def stats_summary() -> list[dict[str, Any]]:
    """Har bir link × promo juftligi: QR bo'lmasa ham 0 yuklanish bilan chiqadi."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT l.url AS link_url,
                   l.title AS link_title,
                   p.code AS promo_code,
                   COALESCE(t.clicks, 0)::bigint AS clicks
            FROM links l
            CROSS JOIN promos p
            LEFT JOIN track_entries t
              ON t.link_id = l.id AND t.promo_id = p.id
            ORDER BY LOWER(p.code), l.url ASC
            """
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = _row(r)
            d["clicks"] = int(d["clicks"])
            out.append(d)
        return out


async def stats_for_link(link_id: int) -> list[dict[str, Any]]:
    """Bitta link uchun: har bir promo va yuklanishlar (track bo'lmasa 0)."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.code AS promo_code,
                   COALESCE(t.clicks, 0)::bigint AS clicks
            FROM promos p
            LEFT JOIN track_entries t
              ON t.promo_id = p.id AND t.link_id = $1
            ORDER BY LOWER(p.code)
            """,
            link_id,
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = _row(r)
            d["clicks"] = int(d["clicks"])
            out.append(d)
        return out


async def stats_for_group(group_id: int) -> list[dict[str, Any]]:
    """Bitta guruh uchun: har bir promo va jami yuklanishlar."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id AS promo_id,
                   p.code AS promo_code,
                   COALESCE(SUM(t.clicks), 0)::bigint AS clicks
            FROM promos p
            LEFT JOIN track_entries t
              ON t.promo_id = p.id
            WHERE p.group_id = $1
            GROUP BY p.id, p.code
            ORDER BY COALESCE(SUM(t.clicks), 0) DESC, LOWER(p.code)
            """,
            group_id,
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = _row(r)
            d["clicks"] = int(d["clicks"])
            out.append(d)
        return out


async def stats_summary_by_group() -> list[dict[str, Any]]:
    """Barcha guruhlar bo'yicha promo kesimidagi jami yuklanishlar (prioritet tartibida)."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT g.id AS group_id,
                   g.name AS group_name,
                   g.priority,
                   p.code AS promo_code,
                   COALESCE(SUM(t.clicks), 0)::bigint AS clicks
            FROM promos p
            JOIN promo_groups g ON g.id = p.group_id
            LEFT JOIN track_entries t ON t.promo_id = p.id
            GROUP BY g.id, g.name, g.priority, p.code
            ORDER BY g.priority ASC, LOWER(g.name), LOWER(p.code)
            """
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = _row(r)
            d["clicks"] = int(d["clicks"])
            out.append(d)
        return out


async def stats_group_totals_desc() -> list[dict[str, Any]]:
    """Guruhlar bo'yicha jami yuklanishlar (prioritet tartibida)."""
    assert _pool is not None
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT g.name AS group_name,
                   g.priority,
                   COUNT(DISTINCT p.id)::bigint AS promo_count,
                   COALESCE(SUM(t.clicks), 0)::bigint AS clicks
            FROM promo_groups g
            LEFT JOIN promos p ON p.group_id = g.id
            LEFT JOIN track_entries t ON t.promo_id = p.id
            GROUP BY g.id, g.name, g.priority
            ORDER BY g.priority ASC, LOWER(g.name)
            """
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = _row(r)
            d["clicks"] = int(d["clicks"])
            d["promo_count"] = int(d["promo_count"])
            out.append(d)
        return out
