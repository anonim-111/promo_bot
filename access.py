"""Bot rollari: super (env) | admin (DB) | viewer (DB + kategoriyalar)."""

from __future__ import annotations

import db
from config import is_super_admin

ROLE_SUPER = "super"
ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"


async def get_role(user_id: int) -> str | None:
    if is_super_admin(user_id):
        return ROLE_SUPER
    user = await db.get_bot_user(user_id)
    if not user:
        return None
    role = str(user.get("role") or "")
    if role in (ROLE_ADMIN, ROLE_VIEWER):
        return role
    return None


async def can_manage(user_id: int) -> bool:
    """CRUD / QR / link / promo — super yoki DB admin."""
    role = await get_role(user_id)
    return role in (ROLE_SUPER, ROLE_ADMIN)


async def can_view_stats(user_id: int) -> bool:
    role = await get_role(user_id)
    return role in (ROLE_SUPER, ROLE_ADMIN, ROLE_VIEWER)


async def can_manage_users(user_id: int) -> bool:
    return is_super_admin(user_id)


async def category_scope(user_id: int) -> set[int] | None:
    """None = barcha kategoriyalar; set = faqat shular (viewer)."""
    if is_super_admin(user_id):
        return None
    user = await db.get_bot_user(user_id)
    if not user:
        return set()
    if user["role"] == ROLE_ADMIN:
        return None
    if user["role"] == ROLE_VIEWER:
        return await db.list_viewer_category_ids(user_id)
    return set()


async def can_access_category(user_id: int, category_id: int) -> bool:
    scope = await category_scope(user_id)
    if scope is None:
        return True
    return int(category_id) in scope


async def can_access_group(user_id: int, group_id: int) -> bool:
    group = await db.get_group(group_id)
    if not group:
        return False
    return await can_access_category(user_id, int(group["category_id"]))
