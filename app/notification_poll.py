"""Periodic read of phone notifications.

The read is a single adb shell command against the live device. It does not
touch the screen, does not open the app and does not change any notification
state, so it is safe to run on a timer.
"""

import asyncio
import time

from astrbot.api import logger
from astrbot.core.computer.computer_client import get_booter, session_booter

from .event_ledger import parse_notifications, to_events

ADB = '/workspace/.local/adbdl/platform-tools/adb'
DEVICE = '172.22.0.2:5555'
COMMAND = f'{ADB} -s {DEVICE} shell dumpsys notification --noredact'
MIN_INTERVAL = 60


async def fetch_notifications(context):
    """Read the dump from whichever sandbox is already alive.

    Nothing is created here. If no session has a booter yet, the poll is
    skipped rather than starting a new sandbox in the background.
    """
    for session_id in list(session_booter):
        try:
            booter = await get_booter(context, session_id)
            result = await booter.shell.exec(COMMAND, timeout=30)
        except Exception as exc:
            logger.warning('Notification poll failed for %s: %s', session_id, type(exc).__name__)
            continue
        if isinstance(result, dict):
            return result.get('stdout') or ''
        return str(result or '')
    return None


class NotificationPoller:
    def __init__(self, fetch, record, interval=600):
        self.fetch = fetch
        self.record = record
        self.interval = max(MIN_INTERVAL, int(interval))
        self.task = None
        self.last_added = None

    async def run_once(self):
        dump = await self.fetch()
        if not dump:
            return None
        added = self.record(dump)
        self.last_added = added
        return added

    async def _loop(self):
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('Notification ledger write failed: %s', type(exc).__name__)
            await asyncio.sleep(self.interval)

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None


def record_dump(ledger, dump, now=None):
    events = to_events(parse_notifications(dump))
    added = ledger.record(events, int(time.time()) if now is None else int(now))
    if added:
        logger.info('Notification ledger: %d new event(s)', added)
    return added
