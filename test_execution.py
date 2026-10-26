import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.agent.tool import ToolSet

from .evidence import EvidenceHooks
from .execution import IndependentExecution


def sanitize(text: str) -> str:
    return text.replace('test-secret', '[redacted]')


class FakeContext:
    def __init__(self, *, fail=False, empty=False, missing_end=False):
        self.history = [{'role': 'user', 'content': 'private-main-history'}]
        self.fail = fail
        self.empty = empty
        self.missing_end = missing_end
        self.received = None

    async def tool_loop_agent(self, **kwargs):
        self.received = kwargs
        kwargs['contexts'].append({'role': 'user', 'content': 'child-only'})
        hooks = kwargs['agent_hooks']
        tool = SimpleNamespace(name='fake_observe')
        await hooks.on_agent_begin(None)
        await hooks.on_tool_start(None, tool, {'token': 'test-secret'})
        if self.fail:
            raise RuntimeError('test-secret')
        if not self.missing_end:
            await hooks.on_tool_end(None, tool, {}, None)
        response = SimpleNamespace(completion_text='' if self.empty else '我已发送。test-secret')
        await hooks.on_agent_done(None, response)
        return response


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.execution = IndependentExecution(self.root, sanitize)

    async def run_fake(self, context, task_contexts=None):
        return await self.execution.run(
            context=context, event=object(), provider_id='fake', goal='observe',
            system_prompt='caller-provided-persona', tools=ToolSet(),
            task_contexts=task_contexts if task_contexts is not None else [],
            allowed_tool_names=frozenset(),
        )

    async def test_independent_messages_and_unverified_claim(self):
        context = FakeContext()
        task_contexts = [{'role': 'user', 'content': 'selected-task-memory'}]
        result = await self.run_fake(context, task_contexts)
        self.assertEqual(context.history, [{'role': 'user', 'content': 'private-main-history'}])
        self.assertEqual(len(task_contexts), 1)
        self.assertEqual(len(context.received['contexts']), 2)
        self.assertEqual(result['external_action_status'], 'unverified')
        self.assertNotIn('timeline', result)
        self.assertNotIn('test-secret', json.dumps(result))
        timeline = (Path(result['raw_record']) / 'timeline.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('private-main-history', timeline)
        self.assertNotIn('test-secret', timeline)
        records = [json.loads(line) for line in timeline.splitlines()]
        calls = [record['call_id'] for record in records if record['kind'].startswith('tool_')]
        self.assertEqual(calls, ['call-1', 'call-1'])

    async def test_exception_keeps_unknown_and_missing_end(self):
        result = await self.run_fake(FakeContext(fail=True))
        self.assertEqual(result['execution_status'], 'failed')
        self.assertEqual(result['external_action_status'], 'unknown')
        self.assertIn('tool_end_missing', result['evidence_gaps'])
        self.assertTrue((Path(result['raw_record']) / 'result.json').exists())

    async def test_empty_response_is_not_success(self):
        result = await self.run_fake(FakeContext(empty=True))
        self.assertEqual(result['execution_status'], 'failed')

    async def test_missing_tool_end_is_visible(self):
        result = await self.run_fake(FakeContext(missing_end=True))
        self.assertIn('tool_end_missing', result['evidence_gaps'])

    async def test_new_tasks_do_not_overwrite_previous_records(self):
        first = await self.run_fake(FakeContext())
        previous = (Path(first['raw_record']) / 'result.json').read_bytes()
        second = await self.run_fake(FakeContext())
        self.assertNotEqual(first['task_id'], second['task_id'])
        self.assertEqual((Path(first['raw_record']) / 'result.json').read_bytes(), previous)

    async def test_string_history_is_rejected_before_execution(self):
        context = FakeContext()
        with self.assertRaises(TypeError):
            await self.run_fake(context, 'serialized-history')
        self.assertIsNone(context.received)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_invalid_message_is_rejected_before_execution(self):
        context = FakeContext()
        with self.assertRaises(TypeError):
            await self.run_fake(context, ['not-a-message'])
        self.assertIsNone(context.received)

    async def test_tools_require_explicit_allowlist(self):
        tools = ToolSet()
        tools.tools.append(SimpleNamespace(name='not-authorized'))
        with self.assertRaises(ValueError):
            await self.execution.run(
                context=FakeContext(), event=object(), provider_id='fake',
                goal='observe', system_prompt='persona', task_contexts=[],
                tools=tools, allowed_tool_names=frozenset(),
            )

    async def test_ambiguous_same_name_results_are_not_guessed(self):
        hooks = EvidenceHooks(self.root, sanitize)
        tool = SimpleNamespace(name='observe')
        await hooks.on_tool_start(None, tool, {})
        await hooks.on_tool_start(None, tool, {})
        await hooks.on_tool_end(None, tool, {}, None)
        self.assertEqual(len(hooks.pending), 2)
        records = (hooks.directory / 'timeline.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertIsNone(json.loads(records[-1])['call_id'])
        self.assertIn('tool_end_missing', hooks.gaps())

    async def test_cancel_is_recorded_and_propagated(self):
        class CancelContext:
            async def tool_loop_agent(self, **kwargs):
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.run_fake(CancelContext())
        result_files = list(self.root.glob('*/result.json'))
        self.assertEqual(len(result_files), 1)
        result = json.loads(result_files[0].read_text(encoding='utf-8'))
        self.assertEqual(result['execution_status'], 'cancelled')
        self.assertEqual(result['external_action_status'], 'unknown')
