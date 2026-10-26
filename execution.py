from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any, Callable

from astrbot.core.agent.message import Message
from astrbot.core.agent.tool import ToolSet

from .evidence import EvidenceHooks


class IndependentExecution:
    def __init__(self, evidence_root: Path, sanitize_text: Callable[[str], str]) -> None:
        self.evidence_root = evidence_root
        self.sanitize_text = sanitize_text
        self._lock = asyncio.Lock()

    async def run(
        self, *, context: Any, event: Any, provider_id: str, goal: str,
        system_prompt: str, tools: ToolSet, task_contexts: list,
        allowed_tool_names: frozenset[str],
        max_steps: int = 10, tool_call_timeout: int = 120,
    ) -> dict:
        if not isinstance(task_contexts, list):
            raise TypeError('task_contexts_must_be_list')
        if not isinstance(tools, ToolSet):
            raise TypeError('tools_must_be_explicit_toolset')
        if not isinstance(allowed_tool_names, frozenset) or not all(
            isinstance(name, str) for name in allowed_tool_names
        ):
            raise TypeError('allowed_tool_names_must_be_frozenset_of_strings')
        for value in (provider_id, goal, system_prompt):
            if not isinstance(value, str) or not value.strip():
                raise ValueError('provider_goal_and_persona_must_be_nonempty_strings')
        if type(max_steps) is not int or max_steps <= 0:
            raise ValueError('max_steps_must_be_positive_integer')
        if type(tool_call_timeout) is not int or tool_call_timeout <= 0:
            raise ValueError('tool_call_timeout_must_be_positive_integer')
        selected_tools = list(tools.tools)
        if any(tool.name not in allowed_tool_names for tool in selected_tools):
            raise ValueError('tool_outside_explicit_allowlist')
        isolated_contexts = []
        for message in task_contexts:
            if not isinstance(message, (dict, Message)):
                raise TypeError('task_context_message_must_be_dict_or_message')
            isolated_contexts.append(Message.model_validate(
                message.model_dump() if isinstance(message, Message) else copy.deepcopy(message)
            ))
        async with self._lock:
            hooks = EvidenceHooks(self.evidence_root, self.sanitize_text)
            hooks.append('task_created', goal=goal)
            try:
                response = await context.tool_loop_agent(
                    event=event,
                    chat_provider_id=provider_id,
                    prompt=goal,
                    system_prompt=system_prompt,
                    tools=ToolSet(selected_tools),
                    contexts=isolated_contexts,
                    agent_hooks=hooks,
                    max_steps=max_steps,
                    tool_call_timeout=tool_call_timeout,
                )
                report = response.completion_text
                if not isinstance(report, str) or not report.strip():
                    raise ValueError('missing_final_model_report')
                hooks.append('loop_returned', model_report=report)
                sanitized_report = self.sanitize_text(report)
                result = {
                    'task_id': hooks.task_id,
                    'execution_status': 'recording_failed' if hooks.recording_failed else 'loop_returned',
                    'evidence_status': 'incomplete',
                    'external_action_status': 'unknown' if hooks.recording_failed else 'unverified',
                    'model_report_excerpt': sanitized_report[:1200],
                    'model_report_truncated': len(sanitized_report) > 1200,
                    'tool_error_seen': hooks.tool_error_seen,
                    'evidence_gaps': hooks.gaps(),
                    'raw_record': str(hooks.directory),
                }
            except asyncio.CancelledError:
                hooks.append('task_cancelled', outcome='unknown')
                hooks.save_result({
                    'task_id': hooks.task_id,
                    'execution_status': 'cancelled',
                    'external_action_status': 'unknown',
                    'evidence_gaps': hooks.gaps(),
                })
                raise
            except Exception as exc:
                hooks.append('task_error', error_type=type(exc).__name__)
                result = {
                    'task_id': hooks.task_id,
                    'execution_status': 'failed',
                    'external_action_status': 'unknown',
                    'error_type': type(exc).__name__,
                    'evidence_gaps': hooks.gaps(),
                    'raw_record': str(hooks.directory),
                }
            hooks.save_result(result)
            return result
