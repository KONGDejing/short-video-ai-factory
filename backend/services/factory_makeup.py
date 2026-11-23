"""Date-bound Douyin release exceptions recorded by the owner."""
from datetime import date, timedelta, timezone
from zoneinfo import ZoneInfo

from models.factory import FactoryRelease, FactoryWorkOrder


def authorized_makeup(db, run, release_day):
    """Return the source day and cap for a release explicitly named by the owner."""
    if run.target_count != 1:
        return None
    order = db.get(FactoryWorkOrder, f"douyin-makeup:{release_day}")
    if not order or order.kind != "douyin_makeup" or order.status != "authorized":
        return None
    evidence = order.evidence or {}
    release_ids = evidence.get("release_ids")
    if (evidence.get("day") != release_day or not isinstance(release_ids, list)
            or not all(isinstance(identifier, str) for identifier in release_ids)
            or not 5 <= len(release_ids) <= 8 or len(set(release_ids)) != len(release_ids)):
        return None
    source_day = (run.settings_snapshot or {}).get("production_day")
    previous_day = (date.fromisoformat(release_day) - timedelta(days=1)).isoformat()
    if source_day not in (previous_day, release_day):
        return None
    zone = ZoneInfo(run.settings_snapshot.get("timezone", "Asia/Shanghai"))
    for slot in db.query(FactoryRelease).filter_by(run_id=run.id).filter(FactoryRelease.id.in_(release_ids)):
        due_day = slot.due_at.replace(tzinfo=timezone.utc).astimezone(zone).date().isoformat()
        if slot.platforms == ["douyin"] and slot.status != "cancelled" and due_day == release_day:
            return source_day, len(release_ids)
    return None
