"""Background task handles for work that outlives one tool call.

Only the handle is kept here. Records, results and delivery state live in
TaskStore, so a lost handle never loses the account of what was started.
"""

from __future__ import annotations

import asyncio


class BackgroundTasks:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}

    def spawn(self, task_id: str, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine, name=f'operation-context:{task_id}')
        self._tasks[task_id] = task
        task.add_done_callback(lambda finished: self._discard(task_id, finished))
        return task

    def _discard(self, task_id: str, finished: asyncio.Task) -> None:
        if self._tasks.get(task_id) is finished:
            self._tasks.pop(task_id, None)

    def get(self, task_id: str):
        return self._tasks.get(task_id)

    def ids(self) -> list[str]:
        return [task_id for task_id, task in self._tasks.items() if not task.done()]

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


class FollowupInbox:
    """Things the parent wants added to a task that is still running.

    Acceptance is recorded when the text lands here; consumption is recorded
    when the loop actually inserts it before a round. Losing the difference
    would silently drop a correction.
    """

    def __init__(self):
        self._pending: dict[str, list[str]] = {}

    def push(self, task_id: str, text: str) -> None:
        self._pending.setdefault(task_id, []).append(text)

    def drain(self, task_id: str) -> list[str]:
        return self._pending.pop(task_id, [])

    def discard(self, task_id: str) -> None:
        self._pending.pop(task_id, None)
