import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import certifi
from pymongo import ReturnDocument
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from pymongo.errors import DuplicateKeyError

DB_PATH = Path(__file__).resolve().parent / "data" / "promo.db"
DB_NAME = "promo_db"

_client: AsyncMongoClient | None = None


def _mongo_uri() -> str:
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise RuntimeError("MONGODB_URI environment variable is required")
    return uri


def _get_client() -> AsyncMongoClient:
    global _client
    if _client is None:
        _client = AsyncMongoClient(
            _mongo_uri(),
            tlsCAFile=certifi.where(),
        )
    return _client


def _db() -> Any:
    return _get_client()[DB_NAME]


def _links() -> Any:
    return _db()["links"]


def _promos() -> Any:
    return _db()["promos"]


def _tracks() -> Any:
    return _db()["track_entries"]


def _counters() -> Any:
    return _db()["counters"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _next_seq(counter_key: str) -> int:
    doc = await _counters().find_one_and_update(
        {"_id": counter_key},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


def _link_to_dict(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if not doc:
        return None
    return {
        "id": int(doc["_id"]),
        "url": doc["url"],
        "title": doc.get("title"),
        "created_at": doc["created_at"],
        "logo_path": doc.get("logo_path"),
    }


def _promo_to_dict(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(doc["_id"]),
        "code": doc["code"],
        "created_at": doc["created_at"],
    }


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    await _promos().create_index("code", unique=True)
    await _tracks().create_index("token", unique=True)
    await _tracks().create_index([("link_id", 1), ("promo_id", 1)], unique=True)
    await _migrate_schema()


async def _migrate_schema() -> None:
    await _links().update_many(
        {"logo_path": {"$exists": False}},
        {"$set": {"logo_path": None}},
    )


def disk_path_for_link_logo(link_id: int) -> Path:
    """QR markazidagi logo fayli (PNG)."""
    return DB_PATH.parent / "logos" / f"link_{link_id}.png"


async def add_link(url: str, title: str | None = None) -> int:
    lid = await _next_seq("links")
    doc = {
        "_id": lid,
        "url": url.strip(),
        "title": (title or "").strip() or None,
        "created_at": _now(),
        "logo_path": None,
    }
    await _links().insert_one(doc)
    return lid


async def get_link(link_id: int) -> dict[str, Any] | None:
    doc = await _links().find_one({"_id": link_id})
    return _link_to_dict(doc)


async def set_link_logo_path(link_id: int, path: str | None) -> None:
    await _links().update_one({"_id": link_id}, {"$set": {"logo_path": path}})


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
    update: dict[str, Any] = {}
    if url is not None:
        update["url"] = url.strip()
    if title is not _UNSET:
        update["title"] = (str(title).strip() or None) if title is not None else None
    if not update:
        return True
    res = await _links().update_one({"_id": link_id}, {"$set": update})
    return res.matched_count > 0


async def delete_link(link_id: int) -> bool:
    row = await get_link(link_id)
    if not row:
        return False
    lp = (row.get("logo_path") or "").strip()
    if lp:
        Path(lp).unlink(missing_ok=True)
    disk_path_for_link_logo(link_id).unlink(missing_ok=True)
    await _tracks().delete_many({"link_id": link_id})
    await _links().delete_one({"_id": link_id})
    return True


async def add_promo(code: str) -> int:
    code = code.strip()
    pid = await _next_seq("promos")
    doc = {"_id": pid, "code": code, "created_at": _now()}
    try:
        await _promos().insert_one(doc)
    except DuplicateKeyError as e:
        await _counters().update_one({"_id": "promos"}, {"$inc": {"seq": -1}})
        raise sqlite3.IntegrityError("duplicate promo code") from e
    return pid


async def list_links() -> list[dict[str, Any]]:
    cursor = _links().find().sort("_id", -1)
    out: list[dict[str, Any]] = []
    async for doc in cursor:
        d = _link_to_dict(doc)
        if d:
            out.append(d)
    return out


async def list_promos() -> list[dict[str, Any]]:
    cursor = _promos().find().sort("_id", -1)
    out: list[dict[str, Any]] = []
    async for doc in cursor:
        out.append(_promo_to_dict(doc))
    return out


async def get_promo_code_by_id(promo_id: int) -> str | None:
    doc = await _promos().find_one({"_id": promo_id}, projection={"code": 1})
    if not doc:
        return None
    return str(doc["code"])


async def get_track_token(link_id: int, promo_id: int) -> str | None:
    doc = await _tracks().find_one(
        {"link_id": link_id, "promo_id": promo_id},
        projection={"token": 1},
    )
    if not doc:
        return None
    return str(doc["token"])


async def create_track_entry(link_id: int, promo_id: int) -> str:
    existing = await get_track_token(link_id, promo_id)
    if existing:
        return existing
    for _ in range(4):
        token = secrets.token_urlsafe(16).rstrip("=").replace("-", "_")
        tid = await _next_seq("track_entries")
        doc = {
            "_id": tid,
            "link_id": link_id,
            "promo_id": promo_id,
            "token": token,
            "clicks": 0,
            "created_at": _now(),
        }
        try:
            await _tracks().insert_one(doc)
            return token
        except DuplicateKeyError:
            await _counters().update_one(
                {"_id": "track_entries"}, {"$inc": {"seq": -1}}
            )
            again = await get_track_token(link_id, promo_id)
            if again:
                return again
            continue
    raise RuntimeError("track_entries uchun token yaratib bo'lmadi")


async def get_link_url_by_token(token: str) -> str | None:
    tdoc = await _tracks().find_one({"token": token}, projection={"link_id": 1})
    if not tdoc:
        return None
    link = await _links().find_one({"_id": tdoc["link_id"]}, projection={"url": 1})
    if not link:
        return None
    return str(link["url"])


async def increment_click(token: str) -> None:
    await _tracks().update_one({"token": token}, {"$inc": {"clicks": 1}})


async def stats_summary() -> list[dict[str, Any]]:
    links = await list_links()
    promos = await list_promos()
    links_sorted = sorted(links, key=lambda r: r["url"])
    promos_sorted = sorted(promos, key=lambda r: str(r["code"]).lower())

    track_map: dict[tuple[int, int], int] = {}
    async for t in _tracks().find():
        track_map[(int(t["link_id"]), int(t["promo_id"]))] = int(t.get("clicks") or 0)

    rows: list[dict[str, Any]] = []
    for p in promos_sorted:
        for l in links_sorted:
            clicks = track_map.get((int(l["id"]), int(p["id"])), 0)
            rows.append(
                {
                    "link_url": l["url"],
                    "link_title": l.get("title"),
                    "promo_code": p["code"],
                    "clicks": clicks,
                }
            )
    return rows


async def stats_for_link(link_id: int) -> list[dict[str, Any]]:
    promos = await list_promos()
    promos_sorted = sorted(promos, key=lambda r: str(r["code"]).lower())

    track_map: dict[int, int] = {}
    async for t in _tracks().find({"link_id": link_id}):
        track_map[int(t["promo_id"])] = int(t.get("clicks") or 0)

    rows: list[dict[str, Any]] = []
    for p in promos_sorted:
        rows.append(
            {
                "promo_code": p["code"],
                "clicks": track_map.get(int(p["id"]), 0),
            }
        )
    return rows