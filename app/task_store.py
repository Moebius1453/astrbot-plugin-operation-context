"""Persistent task records.

One file per task, written atomically. The record is the identity of a piece
of work: it outlives the tool call that started it and the event that carried
it, but it is not a memory or a persona. Only the short result and the goal
text are kept here; commands and screen dumps stay in the evidence directory.

Nothing here wakes the main conversation. A finished task leaves a record the
main model can read when it chooses to.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ACTIVE_STATUSES = frozenset({'queued', 'running'})
FINISHED_STATUSES = frozenset({
    'completed', 'needs_parent', 'step_limit', 'time_budget',
    'stopped', 'rejected', 'failed', 'cancelled', 'interrupted',
})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def _path(self, task_id: str) -> Path:
        return self.root / f'{task_id}.json'

    def _read(self, task_id: str):
        try:
            data = json.loads(self._path(task_id).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, record: dict) -> None:
        path = self._path(record['task_id'])
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)

    def _records(self) -> list[dict]:
        records = []
        for path in self.root.glob('*.json'):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get('task_id'), str):
                records.append(data)
        return records

    async def create(self, *, umo: str, conversation_id: str | None,
                     kind: str, description: str) -> dict:
        record = {
            'task_id': uuid4().hex,
            'umo': umo,
            'conversation_id': conversation_id,
            'kind': kind,
            'description': description[:200],
            'status': 'queued',
            'summary': None,
            'result': None,
            'created_at': now_iso(),
            'updated_at': now_iso(),
        }
        async with self._lock:
            self._write(record)
        return dict(record)

    async def update(self, task_id: str, **fields) -> dict | None:
        async with self._lock:
            record = self._read(task_id)
            if record is None:
                return None
            record.update(fields)
            record['updated_at'] = now_iso()
            self._write(record)
            return dict(record)

    async def get(self, task_id: str | None) -> dict | None:
        if not isinstance(task_id, str) or not task_id:
            return None
        async with self._lock:
            return self._read(task_id)

    async def list_for(self, umo: str, limit: int = 20) -> list[dict]:
        async with self._lock:
            records = [r for r in self._records() if r.get('umo') == umo]
        records.sort(key=lambda r: r.get('created_at') or '', reverse=True)
        return records[:limit]

    async def interrupt_running(self) -> list[str]:
        """Mark work that was running at shutdown as interrupted.

        It is not replayed: the host cannot know whether the last external
        action happened, so the work is left for a human or the main
        conversation to look at before anything continues.
        """
        interrupted = []
        async with self._lock:
            for record in self._records():
                if record.get('status') not in ACTIVE_STATUSES:
                    continue
                record['status'] = 'interrupted'
                record['summary'] = (
                    'The plugin restarted while this task was running. '
                    'It was not replayed; look at the phone or workspace first.'
                )
                record['updated_at'] = now_iso()
                self._write(record)
                interrupted.append(record['task_id'])
        return interrupted
