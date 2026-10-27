from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from astrbot.core.agent.hooks import BaseAgentRunHooks


class EvidenceHooks(BaseAgentRunHooks):
    def __init__(self, root: Path, sanitize_text: Callable[[str], str]) -> None:
        self.task_id = uuid4().hex
        self.directory = root / self.task_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.sanitize_text = sanitize_text
        self.pending: dict[int, str] = {}
        self.sequence = 0
        self.recording_failed = False
        self.tool_error_seen = False
        self.incomplete_result_seen = False

    def _safe(self, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if value.startswith('data:'):
                self.incomplete_result_seen = True
                return {'omitted': 'inline_data'}
            return self.sanitize_text(value)
        if isinstance(value, (list, tuple)):
            return [self._safe(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                normalized = str(key).lower().replace('-', '_')
                if any(word in normalized for word in (
                    'password', 'secret', 'token', 'authorization', 'cookie',
                    'api_key', 'apikey',
                )):
                    result[str(key)] = '[redacted]'
                elif normalized in {'data', 'blob'}:
                    self.incomplete_result_seen = True
                    result[str(key)] = {'omitted': 'binary_or_inline_payload'}
                else:
                    result[str(key)] = self._safe(item)
            return result
        if hasattr(value, 'model_dump'):
            return self._safe(value.model_dump(mode='json'))
        self.incomplete_result_seen = True
        return {'omitted_type': type(value).__name__}

    def append(self, kind: str, **payload: Any) -> None:
        try:
            record = {
                'at': datetime.now(timezone.utc).isoformat(),
                'kind': kind,
                **self._safe(payload),
            }
            with (self.directory / 'timeline.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
        except Exception:
            self.recording_failed = True
            raise

    def save_result(self, result: dict) -> None:
        try:
            with (self.directory / 'result.json').open('x', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
        except Exception:
            self.recording_failed = True
            raise

    async def on_agent_begin(self, run_context) -> None:
        self.append('agent_begin')

    async def on_tool_start(self, run_context, tool, tool_args) -> None:
        self.sequence += 1
        call_id = f'call-{self.sequence}'
        if self.pending:
            self.incomplete_result_seen = True
        self.pending[self.sequence] = tool.name
        self.append('tool_start', call_id=call_id, tool=tool.name, arguments=tool_args)

    async def on_tool_end(self, run_context, tool, tool_args, tool_result) -> None:
        matches = [number for number, name in self.pending.items() if name == tool.name]
        number = matches[0] if len(matches) == 1 else None
        if number is None:
            self.incomplete_result_seen = True
        else:
            del self.pending[number]
        if tool_result is None:
            self.incomplete_result_seen = True
        elif getattr(tool_result, 'isError', False):
            self.tool_error_seen = True
        # 框架这里只交付最后一个工具结果，不能据此声称保存了完整流。
        self.append(
            'tool_end', call_id=f'call-{number}' if number is not None else None,
            tool=tool.name, last_result=tool_result,
        )

    async def on_agent_done(self, run_context, llm_response) -> None:
        self.append('agent_done', model_report=llm_response.completion_text)

    def gaps(self) -> list[str]:
        gaps = ['hook_exposes_last_tool_result_only', 'resource_references_not_copied']
        if self.pending:
            gaps.append('tool_end_missing')
        if self.recording_failed:
            gaps.append('recording_failed')
        if self.incomplete_result_seen:
            gaps.append('missing_or_omitted_payload')
        return gaps
