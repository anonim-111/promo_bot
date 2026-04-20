import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import os

import libsql_experimental as libsql

# Turso ulanish
TURSO_URL = os.getenv("TURSO_DB_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# Logo fayllari uchun lokal papka (deploy da o'chmaydi chunki faylga saqlanmaydi)
_BASE = Path(__file__).resolve().parent / "data"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn():
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)


async def init_db() -> None:
    conn = _get_conn()
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT,
            created_at TEXT NOT NULL,
            logo_path TEXT
        );

        CREATE TABLE IF NOT EXISTS promos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS track_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER NOT NULL,
            promo_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            clicks INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(link_id, promo_id),
            FOREIGN KEY (link_id) REFERENCES links(id) ON DELETE CASCADE,
            FOREIGN KEY (promo_id) REFERENCES promos(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_track_token ON track_entries(token);
        """
    )
    conn.commit()
    await _migrate_schema()


async def _migrate_schema() -> None:
    conn = _get_conn()
    cur = conn.execute("PRAGMA table_info(links)")
    cols = {row[1] for row in cur.fetchall()}
    if "logo_path" not in cols:
        conn.execute("ALTER TABLE links ADD COLUMN logo_path TEXT")
        conn.commit()


def disk_path_for_link_logo(link_id: int) -> Path:
    """QR markazidagi logo fayli (PNG)."""
    return _BASE / "logos" / f"link_{link_id}.png"


async def add_link(url: str, title: str | None = None) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO links (url, title, created_at, logo_path) VALUES (?, ?, ?, NULL)",
        (url.strip(), (title or "").strip() or None, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


async def get_link(link_id: int) -> dict[str, Any] | None:
    conn = _get_conn()
    cur = conn.execute(
        "SELECT id, url, title, created_at, logo_path FROM links WHERE id = ?",
        (link_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["id", "url", "title", "created_at", "logo_path"]
    return dict(zip(keys, row))


async def set_link_logo_path(link_id: int, path: str | None) -> None:
    conn = _get_conn()
    conn.execute("UPDATE links SET logo_path = ? WHERE id = ?", (path, link_id))
    conn.commit()


_UNSET = object()


async def update_link_fields(
    link_id: int,
    *,
    url: str | None = None,
    title: Any = _UNSET,
) -> bool:
    row = await get_link(link_id)
    if not row:
        return False
    sets: list[str] = []
    vals: list[object] = []
    if url is not None:
        sets.append("url = ?")
        vals.append(url.strip())
    if title is not _UNSET:
        sets.append("title = ?")
        vals.append((str(title).strip() or None) if title is not None else None)
    if not sets:
        return True
    vals.append(link_id)
    conn = _get_conn()
    conn.execute(
        f"UPDATE links SET {', '.join(sets)} WHERE id = ?",
        vals,
    )
    conn.commit()
    return True


async def delete_link(link_id: int) -> bool:
    row = await get_link(link_id)
    if not row:
        return False
    lp = (row.get("logo_path") or "").strip()
    if lp:
        Path(lp).unlink(missing_ok=True)
    disk_path_for_link_logo(link_id).unlink(missing_ok=True)
    conn = _get_conn()
    conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
    conn.commit()
    return True


async def add_promo(code: str) -> int:
    code = code.strip()
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO promos (code, created_at) VALUES (?, ?)",
        (code, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


async def list_links() -> list[dict[str, Any]]:
    conn = _get_conn()
    cur = conn.execute(
        "SELECT id, url, title, created_at, logo_path FROM links ORDER BY id DESC"
    )
    rows = cur.fetchall()
    keys = ["id", "url", "title", "created_at", "logo_path"]
    return [dict(zip(keys, r)) for r in rows]


async def list_promos() -> list[dict[str, Any]]:
    conn = _get_conn()
    cur = conn.execute(
        "SELECT id, code, created_at FROM promos ORDER BY id DESC"
    )
    rows = cur.fetchall()
    keys = ["id", "code", "created_at"]
    return [dict(zip(keys, r)) for r in rows]


async def get_promo_code_by_id(promo_id: int) -> str | None:
    conn = _get_conn()
    cur = conn.execute("SELECT code FROM promos WHERE id = ?", (promo_id,))
    row = cur.fetchone()
    return row[0] if row else None


async def get_track_token(link_id: int, promo_id: int) -> str | None:
    conn = _get_conn()
    cur = conn.execute(
        "SELECT token FROM track_entries WHERE link_id = ? AND promo_id = ?",
        (link_id, promo_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


async def create_track_entry(link_id: int, promo_id: int) -> str:
    existing = await get_track_token(link_id, promo_id)
    if existing:
        return existing
    for _ in range(4):
        token = secrets.token_urlsafe(16).rstrip("=").replace("-", "_")
        try:
            conn = _get_conn()
            conn.execute(
                """
                INSERT INTO track_entries (link_id, promo_id, token, clicks, created_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (link_id, promo_id, token, _now()),
            )
            conn.commit()
            return token
        except Exception:
            again = await get_track_token(link_id, promo_id)
            if again:
                return again
            continue
    raise RuntimeError("track_entries uchun token yaratib bo'lmadi")


async def get_link_url_by_token(token: str) -> str | None:
    conn = _get_conn()
    cur = conn.execute(
        "SELECT l.url FROM track_entries t JOIN links l ON l.id = t.link_id WHERE t.token = ?",
        (token,),
    )
    row = cur.fetchone()
    return row[0] if row else None


async def increment_click(token: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE track_entries SET clicks = clicks + 1 WHERE token = ?", (token,)
    )
    conn.commit()


async def stats_summary() -> list[dict[str, Any]]:
    conn = _get_conn()
    cur = conn.execute(
        """
        SELECT l.url AS link_url,
               l.title AS link_title,
               p.code AS promo_code,
               COALESCE(t.clicks, 0) AS clicks
        FROM links l
        CROSS JOIN promos p
        LEFT JOIN track_entries t
          ON t.link_id = l.id AND t.promo_id = p.id
        ORDER BY p.code ASC, l.url ASC
        """
    )
    rows = cur.fetchall()
    keys = ["link_url", "link_title", "promo_code", "clicks"]
    return [dict(zip(keys, r)) for r in rows]


async def stats_for_link(link_id: int) -> list[dict[str, Any]]:
    conn = _get_conn()
    cur = conn.execute(
        """
        SELECT p.code AS promo_code,
               COALESCE(t.clicks, 0) AS clicks
        FROM promos p
        LEFT JOIN track_entries t
          ON t.promo_id = p.id AND t.link_id = ?
        ORDER BY p.code ASC
        """,
        (link_id,),
    )
    rows = cur.fetchall()
    keys = ["promo_code", "clicks"]
    return [dict(zip(keys, r)) for r in rows]