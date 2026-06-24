"""Планировщик: проверка проектов несколько раз в день (утро/день/вечер)."""
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config, database, jobs

_scheduler: BackgroundScheduler | None = None

CHECK_KEYS = {
    "check_time_morning": config.DEFAULT_CHECK_MORNING,
    "check_time_noon": config.DEFAULT_CHECK_NOON,
    "check_time_evening": config.DEFAULT_CHECK_EVENING,
}


def _tz() -> ZoneInfo:
    name = database.get_setting("timezone", config.TIMEZONE) or config.TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def _parse_hm(value: str, fallback: str) -> tuple[int, int]:
    try:
        h, m = value.strip().split(":")
        return int(h), int(m)
    except Exception:
        h, m = fallback.split(":")
        return int(h), int(m)


def reschedule() -> None:
    """Пересоздаёт задания проверок согласно текущим настройкам."""
    if _scheduler is None:
        return
    # Удаляем прежние задания проверок.
    for job in _scheduler.get_jobs():
        if job.id.startswith("check_"):
            job.remove()

    tz = _tz()
    for key, default in CHECK_KEYS.items():
        value = database.get_setting(key, default) or default
        hour, minute = _parse_hm(value, default)
        _scheduler.add_job(
            lambda: jobs.run_check_and_notify(force=False),
            trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
            id=f"check_{key}", replace_existing=True, misfire_grace_time=3600,
        )


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=_tz())
    _scheduler.start()
    reschedule()


def next_check() -> str | None:
    """Ближайшее время следующей проверки среди всех заданий."""
    if _scheduler is None:
        return None
    times = [j.next_run_time for j in _scheduler.get_jobs()
             if j.id.startswith("check_") and j.next_run_time]
    if not times:
        return None
    return min(times).strftime("%d.%m.%Y %H:%M %Z")


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
