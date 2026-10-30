"""Persistent work history for one task.

The executor already keeps a real conversation inside a single run. This file
keeps that conversation on disk so a later round can continue the same work
instead of starting blind.

It holds only the run's own messages: what the executor was told, what it ran
and what it saw. It is not a persona and it never includes the main
conversation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

FORMAT_VERSION = 1


def serialize(messages) -> list[dict]:
    return [
        item if isinstance(item, dict) else item.model_dump()
        for item in messages
    ]


class WorkContext:
    def __init__(self, root: Path, task_id: str):
        self.root = Path(root)
        self.task_id = task_id
        self.path = self.root / f'{task_id}.json'

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        if not isinstance(data, dict) or data.get('version') != FORMAT_VERSION:
            return []
        messages = data.get('messages')
        if not isinstance(messages, list):
            return []
        return [item for item in messages if isinstance(item, dict)]

    def save(self, messages) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {'version': FORMAT_VERSION, 'messages': serialize(messages)}
        temporary = self.path.with_name(self.path.name + '.tmp')
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False), encoding='utf-8',
        )
        os.replace(temporary, self.path)
