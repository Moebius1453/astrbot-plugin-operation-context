import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ..app.adapter import CAPABILITIES
from ..main import OperationContextPlugin


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        context = SimpleNamespace(
            add_llm_tools=Mock(),
            get_config=Mock(return_value={'provider_settings': {'tool_call_timeout': 120}}),
        )
        with patch('operation_test.main.StarTools.get_data_dir', return_value=Path(temporary.name)):
            self.plugin = OperationContextPlugin(context, {'allowed_capabilities': sorted(CAPABILITIES)})
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=SimpleNamespace(
            unified_msg_origin='test', get_extra=lambda key: ['astrbot_execute_shell'],
        )))
        backend = patch('operation_test.main.build_backend', return_value=object())
        self.backend = backend.start()
        self.addCleanup(backend.stop)

    async def execute(self, action, **kwargs):
        return json.loads(await self.plugin.execute(
            self.run_context, kwargs.pop('goal', '任务目标'), actions=[action], **kwargs,
        ))

    async def test_publish_passes_parent_goal_and_frozen_payload(self):
        self.plugin.run_agent = AsyncMock(return_value={
            'status': 'completed', 'summary': '已准备', 'command_count': 2, 'raw_record': 'evidence',
        })
        submit = AsyncMock(return_value={'status': 'confirmed'})
        self.plugin.submitter = AsyncMock(return_value=SimpleNamespace(submit=submit))
        result = await self.execute('post_publish', goal='发到星空话题，不加配图', post_title='标题', post_text='原文')
        arguments = self.plugin.run_agent.call_args
        self.assertIn('发到星空话题，不加配图', arguments.args[1])
        self.assertEqual(arguments.kwargs['frozen_files'], {'title': '标题', 'body': '原文'})
        self.assertIn('host performs the final commit', arguments.kwargs['extra_rules'])
        submit.assert_awaited_once_with('标题', '原文')
        self.assertEqual(result['submit']['status'], 'confirmed')

    async def test_comment_keeps_target_and_submission_lock(self):
        async def prepare(request, goal, **kwargs):
            self.assertTrue(self.plugin.phone_busy)
            self.assertIn('回复星空帖的作者', goal)
            self.assertEqual(kwargs['frozen_text'], '逐字原文')
            return {'status': 'completed', 'summary': '已填', 'command_count': 3, 'raw_record': 'evidence'}

        async def submit(text):
            self.assertTrue(self.plugin.phone_busy)
            self.assertEqual(text, '逐字原文')
            return {'status': 'unknown'}

        self.plugin.run_agent = AsyncMock(side_effect=prepare)
        submitter = SimpleNamespace(submit=AsyncMock(side_effect=submit))
        self.plugin.comment_submitter = AsyncMock(return_value=submitter)
        result = await self.execute('comment_publish', goal='回复星空帖的作者', input_text='逐字原文')
        self.assertEqual(result['submit']['status'], 'unknown')
        self.assertEqual(submitter.submit.await_count, 1)
        self.assertFalse(self.plugin.phone_busy)

    async def test_delete_preserves_parent_goal(self):
        self.plugin.run_agent = AsyncMock(return_value={'status': 'completed'})
        await self.execute('post_delete', goal='只删除昨天的那篇，不动同名新帖', post_title='目标帖')
        arguments = self.plugin.run_agent.call_args
        self.assertIn('只删除昨天的那篇，不动同名新帖', arguments.args[1])
        self.assertEqual(arguments.kwargs['frozen_files'], {'target': '目标帖'})
        self.assertIn('frozen_files.target', arguments.args[1])

    async def test_failed_preparation_never_submits(self):
        self.plugin.run_agent = AsyncMock(return_value={
            'status': 'needs_parent', 'summary': '目标不明', 'command_count': 1, 'raw_record': 'evidence',
        })
        self.plugin.submitter = AsyncMock()
        result = await self.execute('post_publish', post_title='标题', post_text='原文')
        self.assertEqual(result['submit']['status'], 'skipped')
        self.plugin.submitter.assert_not_called()

    async def test_phone_busy_does_not_block_ledger_workspace_or_notes(self):
        self.plugin.phone_busy = True
        for action, kwargs in (
            ('phone_events', {}), ('sandbox_worker', {}),
            ('note_read', {'note_path': 'notes/a.md'}),
            ('note_write', {'note_path': 'notes/b.md'}),
        ):
            handler = AsyncMock(return_value='{"status":"ok"}')
            self.plugin.standalone_handlers[action] = handler
            result = await self.execute(action, **kwargs)
            self.assertEqual(result['status'], 'ok')
            handler.assert_awaited_once()
            self.assertTrue(self.plugin.phone_busy)
        self.plugin.run_fixed = AsyncMock(return_value='{"status":"ok"}')
        self.assertEqual((await self.execute('sandbox_status'))['status'], 'ok')
        self.assertTrue(self.plugin.phone_busy)

    async def test_phone_tasks_are_exclusive(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def run(request):
            started.set()
            await release.wait()
            return '{"status":"completed"}'

        self.plugin.standalone_handlers['phone_agent'] = AsyncMock(side_effect=run)
        first = asyncio.create_task(self.execute('phone_agent'))
        self.addAsyncCleanup(self.cancel_task, first)
        await asyncio.wait_for(started.wait(), 1)
        second = await self.execute('phone_agent')
        self.assertEqual(second['reason'], 'another_phone_task_running')
        release.set()
        self.assertEqual((await first)['status'], 'completed')
        self.assertFalse(self.plugin.phone_busy)
        self.assertEqual(self.plugin.standalone_handlers['phone_agent'].await_count, 1)

    async def test_same_note_is_serialized_but_other_notes_can_run(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def run(request):
            calls.append(request.note_path)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return '{"status":"ok"}'

        self.plugin.standalone_handlers['note_write'] = run
        first = asyncio.create_task(self.execute('note_write', note_path='notes/a.md'))
        self.addAsyncCleanup(self.cancel_task, first)
        await asyncio.wait_for(started.wait(), 1)
        second = asyncio.create_task(self.execute('note_write', note_path='/notes/a.md'))
        self.addAsyncCleanup(self.cancel_task, second)
        other = await self.execute('note_write', note_path='notes/b.md')
        self.assertEqual(other['status'], 'ok')
        self.assertNotIn('/notes/a.md', calls)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(calls, ['notes/a.md', 'notes/b.md', '/notes/a.md'])

    async def test_worker_and_note_mutations_do_not_overlap(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def worker(request):
            started.set()
            await release.wait()
            return '{"status":"ok"}'

        note = AsyncMock(return_value='{"status":"ok"}')
        self.plugin.standalone_handlers['sandbox_worker'] = worker
        self.plugin.standalone_handlers['note_write'] = note
        task = asyncio.create_task(self.execute('sandbox_worker'))
        self.addAsyncCleanup(self.cancel_task, task)
        await asyncio.wait_for(started.wait(), 1)
        waiting = asyncio.create_task(self.execute('note_write', note_path='notes/a.md'))
        self.addAsyncCleanup(self.cancel_task, waiting)
        ledger = AsyncMock(return_value='{"events":[]}')
        self.plugin.standalone_handlers['phone_events'] = ledger
        await self.execute('phone_events')
        note.assert_not_awaited()
        release.set()
        await asyncio.gather(task, waiting)
        note.assert_awaited_once()

    async def test_cancel_releases_phone(self):
        started = asyncio.Event()

        async def run(request):
            started.set()
            await asyncio.Event().wait()

        self.plugin.standalone_handlers['phone_agent'] = run
        task = asyncio.create_task(self.execute('phone_agent'))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.plugin.phone_busy)

    async def test_errors_keep_reason_but_not_raw_output(self):
        self.plugin.standalone_handlers['note_read'] = AsyncMock(
            side_effect=ValueError('note_read_failed: PRIVATE stderr and commands'),
        )
        result = await self.execute('note_read', note_path='notes/a.md')
        self.assertEqual(result['reason'], 'note_read_failed')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(result['external_action_status'], 'unknown')

    async def test_invalid_goal_and_capability_return_reason(self):
        result = await self.execute('phone_agent', goal=' ')
        self.assertEqual(result['reason'], 'invalid_goal')
        result = await self.execute('qq')
        self.assertEqual(result['reason'], 'capability_not_authorized')
        self.backend.assert_not_called()

    async def test_unexpected_exception_returns_failure_without_details(self):
        self.plugin.standalone_handlers['phone_agent'] = AsyncMock(side_effect=RuntimeError('PRIVATE raw output'))
        result = await self.execute('phone_agent')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'execution_failed')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertFalse(self.plugin.phone_busy)

    async def cancel_task(self, task):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
