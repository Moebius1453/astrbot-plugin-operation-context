import json
import re
from pathlib import Path

TASK_ID = re.compile(r'^[a-f0-9]{32}$')


async def refresh_task_receipts(contexts, store, umo, conversation_id):
    updated = list(contexts)
    for index, message in enumerate(contexts):
        if message.get('role') != 'tool' or not isinstance(message.get('content'), str):
            continue
        try:
            receipt = json.loads(message['content'])
        except ValueError:
            continue
        if not isinstance(receipt, dict) or receipt.get('status') != 'dispatched':
            continue
        task_id = receipt.get('task_id')
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            continue
        record = await store.get(task_id)
        if (record is None or record.get('umo') != umo
                or record.get('conversation_id') != conversation_id):
            continue
        receipt['current_task'] = {
            key: record.get(key) for key in ('status', 'summary', 'updated_at', 'artifacts')
        }
        result = record.get('result')
        if isinstance(result, dict):
            receipt['current_task']['evidence'] = result.get('raw_record')
        updated[index] = {**message, 'content': json.dumps(receipt, ensure_ascii=False)}
    return updated


def read_task_evidence(data_dir: Path, record: dict) -> dict:
    task_id = record['task_id']
    evidence = (record.get('result') or {}).get('raw_record')
    path = None
    if isinstance(evidence, str):
        candidate = Path(evidence).resolve()
        allowed = [(data_dir / name).resolve() for name in ('phone_agent', 'sandbox_worker')]
        if any(candidate.is_relative_to(root) for root in allowed):
            path = candidate / 'commands.jsonl'
    if path is None:
        work_path = data_dir / 'work_contexts' / f'{task_id}.json'
        if work_path.exists():
            data = json.loads(work_path.read_text(encoding='utf-8'))
            tools = [m for m in data.get('messages', []) if m.get('role') == 'tool']
            return {'record': record, 'events': tools[-12:], 'source': 'work_context'}
        return {'record': record, 'events': [], 'source': None}
    events = []
    if path.is_file():
        with path.open(encoding='utf-8') as stream:
            from collections import deque
            for line in deque(stream, maxlen=30):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    return {'record': record, 'events': events, 'source': 'evidence'}
