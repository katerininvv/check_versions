"""Планировщик: еженедельная рассылка (Вс 12:00 MSK) + периодический опрос."""
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config, database, jobs

_scheduler: BackgroundScheduler | None = None


def _tz() -> ZoneInfo:
    name = database.get_setting("timezone", config.TIMEZONE) or config.TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def reschedule() -> None:
    """Пересоздаёт задания согласно текущим настройкам."""
    if _scheduler is None:
        return
    for job_id in ("broadcast", "poll"):
        job = _scheduler.get_job(job_id)
        if job:
            job.remove()

    day = database.get_setting("schedule_day", config.DEFAULT_SCHEDULE_DAY)
    hour = int(database.get_setting("schedule_hour", str(config.DEFAULT_SCHEDULE_HOUR)))
    minute = int(database.get_setting("schedule_minute", str(config.DEFAULT_SCHEDULE_MINUTE)))
    interval = int(database.get_setting("poll_interval", str(config.DEFAULT_POLL_INTERVAL)))

    _scheduler.add_job(
        lambda: jobs.run_broadcast(force=False),
        trigger=CronTrigger(day_of_week=day, hour=hour, minute=minute, timezone=_tz()),
        id="broadcast", replace_existing=True, misfire_grace_time=3600,
    )
    _scheduler.add_job(
        jobs.run_poll,
        trigger=IntervalTrigger(minutes=max(5, interval)),
        id="poll", replace_existing=True, misfire_grace_time=600,
    )


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=_tz())
    _scheduler.start()
    reschedule()


def next_broadcast() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("broadcast")
    if job and job.next_run_time:
        return job.next_run_time.strftime("%d.%m.%Y %H:%M %Z")
    return None


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
