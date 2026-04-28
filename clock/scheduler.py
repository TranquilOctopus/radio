"""
Alarm scheduler.

Uses APScheduler to fire the alarm daily at the configured local wall-clock
time.  The scheduler picks up the system timezone via tzlocal.get_localzone()
(reading /etc/localtime), so DST transitions are handled by APScheduler's
CronTrigger inside the resolved IANA zone.  Set the system zone with
`sudo timedatectl set-timezone <Region/City>`.
"""
import logging
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_ALARM_JOB_ID = "alarm"
_SNOOZE_JOB_ID = "snooze"


class AlarmScheduler:
    def __init__(self, on_alarm: Callable, on_snooze_end: Callable):
        self._on_alarm = on_alarm
        self._on_snooze_end = on_snooze_end
        self._scheduler = BackgroundScheduler()
        self._alarm_time: str | None = None          # "HH:MM"
        self._snooze_active = False
        self._scheduler.start()
        logger.info("Alarm scheduler started")

    # ------------------------------------------------------------------
    # Alarm management
    # ------------------------------------------------------------------

    def set_alarm(self, time_str: str) -> None:
        """Schedule a daily alarm at 'HH:MM' local time."""
        try:
            dt = datetime.strptime(time_str, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"Invalid time format '{time_str}', expected HH:MM") from exc

        self._alarm_time = time_str
        self._remove_job(_ALARM_JOB_ID)
        self._scheduler.add_job(
            self._on_alarm,
            CronTrigger(hour=dt.hour, minute=dt.minute),
            id=_ALARM_JOB_ID,
            name="Daily alarm",
            replace_existing=True,
            misfire_grace_time=60,
        )
        logger.info("Alarm set for %s (daily)", time_str)

    def clear_alarm(self) -> None:
        self._alarm_time = None
        self._remove_job(_ALARM_JOB_ID)
        self.cancel_snooze()
        logger.info("Alarm cleared")

    @property
    def alarm_time(self) -> str | None:
        return self._alarm_time

    @property
    def alarm_enabled(self) -> bool:
        return self._alarm_time is not None and self._scheduler.get_job(_ALARM_JOB_ID) is not None

    # ------------------------------------------------------------------
    # Snooze
    # ------------------------------------------------------------------

    def snooze(self, duration_minutes: int) -> None:
        """Pause the alarm and reschedule it `duration_minutes` from now."""
        self._snooze_active = True
        fire_at = datetime.now() + timedelta(minutes=duration_minutes)
        self._remove_job(_SNOOZE_JOB_ID)
        self._scheduler.add_job(
            self._on_snooze_end,
            "date",
            run_date=fire_at,
            id=_SNOOZE_JOB_ID,
            name="Snooze",
        )
        logger.info("Snooze: alarm will resume in %d min at %s", duration_minutes, fire_at.strftime("%H:%M"))

    def cancel_snooze(self) -> None:
        self._snooze_active = False
        self._remove_job(_SNOOZE_JOB_ID)

    @property
    def snooze_active(self) -> bool:
        return self._snooze_active

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    def _remove_job(self, job_id: str) -> None:
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
