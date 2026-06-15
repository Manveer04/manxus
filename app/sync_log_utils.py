from __future__ import annotations

import re
from typing import Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import SyncLog


def _sql_like_to_regex(pattern: str) -> str:
    parts: list[str] = []
    for char in pattern:
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    return f"^{''.join(parts)}$"


def _sync_log_matches(
    log: SyncLog,
    *,
    platform: Optional[str] = None,
    action: Optional[str] = None,
    product_ids: Optional[Sequence[int]] = None,
    message_like: Optional[str] = None,
    created_after=None,
) -> bool:
    if platform is not None and log.platform != platform:
        return False
    if action is not None and log.action != action:
        return False
    if product_ids is not None and log.product_id not in set(product_ids):
        return False
    if created_after is not None:
        if log.created_at is None or log.created_at < created_after:
            return False
    if message_like is not None:
        message = log.message or ""
        if not re.match(_sql_like_to_regex(message_like), message):
            return False
    return True


def get_recent_sync_logs(
    db: Session,
    *,
    limit: int,
    platform: Optional[str] = None,
    action: Optional[str] = None,
    product_ids: Optional[Sequence[int]] = None,
    message_like: Optional[str] = None,
    created_after=None,
) -> list[SyncLog]:
    """Fetch the newest sync logs without relying on ORDER BY on the table."""
    if limit <= 0:
        return []

    max_id = db.query(func.max(SyncLog.id)).scalar()
    if not max_id:
        return []

    product_id_set = set(product_ids) if product_ids is not None else None
    results: list[SyncLog] = []

    for log_id in range(int(max_id), 0, -1):
        try:
            log = db.query(SyncLog).filter(SyncLog.id == log_id).one_or_none()
        except Exception:
            continue

        if log is None:
            continue

        if not _sync_log_matches(
            log,
            platform=platform,
            action=action,
            product_ids=product_id_set,
            message_like=message_like,
            created_after=created_after,
        ):
            continue

        results.append(log)
        if len(results) >= limit:
            break

    return results


def get_latest_sync_log(
    db: Session,
    *,
    platform: Optional[str] = None,
    action: Optional[str] = None,
    product_ids: Optional[Sequence[int]] = None,
    message_like: Optional[str] = None,
    created_after=None,
) -> Optional[SyncLog]:
    logs = get_recent_sync_logs(
        db,
        limit=1,
        platform=platform,
        action=action,
        product_ids=product_ids,
        message_like=message_like,
        created_after=created_after,
    )
    return logs[0] if logs else None