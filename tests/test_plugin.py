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
        self.conversations = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value='conversation-1'),
            get_conversation=AsyncMock(return_value=None),
            update_conversation=AsyncMock(),
        )
        context = SimpleNamespace(
            add_llm_tools=Mock(),
            conversation_manager=self.conversations,
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

    async def wait_background(self, task_id):
        task = self.plugin.background.get(task_id)
        if task is not None:
            await task
        return await self.plugin.tasks.get(task_id)

    async def test_publish_passes_parent_goal_and_frozen_payload(self):
        self.plugin.run_agent = AsyncMock(return_value={
            'status': 'completed', 'summary': '已准备', 'command_count': 2, 'raw_record': 'evidence',
        })
        submit = AsyncMock(return_value={'status': 'confirmed'})
        self.plugin.submitter = AsyncMock(return_value=SimpleNamespace(submit=submit))
        dispatched = await self.execute(
            'post_publish', goal='发到星空话题，不加配图', post_title='标题', post_text='原文',
        )
        self.assertEqual(dispatched['status'], 'dispatched')
        record = await self.wait_background(dispatched['task_id'])
        arguments = self.plugin.run_agent.call_args
        self.assertIn('发到星空话题，不加配图', arguments.args[1])
        self.assertEqual(arguments.kwargs['frozen_files'], {'title': '标题', 'body': '原文'})
        self.assertIn('host performs the final commit', arguments.kwargs['extra_rules'])
        submit.assert_awaited_once_with('标题', '原文')
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['result']['submit']['status'], 'confirmed')

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
        dispatched = await self.execute('comment_publish', goal='回复星空帖的作者', input_text='逐字原文')
        record = await self.wait_background(dispatched['task_id'])
        self.assertEqual(record['result']['submit']['status'], 'unknown')
        self.assertEqual(record['status'], 'needs_parent')
        self.assertEqual(submitter.submit.await_count, 1)
        self.assertFalse(self.plugin.phone_busy)

    async def test_delete_preserves_parent_goal(self):
        self.plugin.run_agent = AsyncMock(return_value={'status': 'completed', 'summary': '已删'})
        dispatched = await self.execute(
            'post_delete', goal='只删除昨天的那篇，不动同名新帖', post_title='目标帖',
        )
        record = await self.wait_background(dispatched['task_id'])
        self.assertEqual(record['status'], 'completed')
        arguments = self.plugin.run_agent.call_args
        self.assertIn('只删除昨天的那篇，不动同名新帖', arguments.args[1])
        self.assertEqual(arguments.kwargs['frozen_files'], {'target': '目标帖'})
        self.assertIn('frozen_files.target', arguments.args[1])

    async def test_failed_preparation_never_submits(self):
        self.plugin.run_agent = AsyncMock(return_value={
            'status': 'needs_parent', 'summary': '目标不明', 'command_count': 1, 'raw_record': 'evidence',
        })
        self.plugin.submitter = AsyncMock()
        dispatched = await self.execute('post_publish', post_title='标题', post_text='原文')
        record = await self.wait_background(dispatched['task_id'])
        self.assertEqual(record['result']['submit']['status'], 'skipped')
        self.assertEqual(record['status'], 'needs_parent')
        self.plugin.submitter.assert_not_called()

    async def test_phone_busy_does_not_block_ledger_workspace_or_notes(self):
        self.plugin.phone_busy = True
        for action, kwargs in (
            ('phone_events', {}),
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
        self.plugin.standalone_handlers['sandbox_worker'] = AsyncMock(return_value='{"status":"ok"}')
        dispatched = await self.execute('sandbox_worker')
        self.assertEqual(dispatched['status'], 'dispatched')
        self.assertTrue(self.plugin.phone_busy)
        await self.wait_background(dispatched['task_id'])

    async def test_phone_tasks_are_exclusive(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def run(request):
            started.set()
            await release.wait()
            return '{"status":"completed"}'

        self.plugin.standalone_handlers['phone_agent'] = run
        first = await self.execute('phone_agent')
        self.assertEqual(first['status'], 'dispatched')
        await asyncio.wait_for(started.wait(), 1)
        second = await self.execute('phone_agent')
        self.assertEqual(second['reason'], 'another_phone_task_running')
        release.set()
        record = await self.wait_background(first['task_id'])
        self.assertEqual(record['status'], 'completed')
        self.assertFalse(self.plugin.phone_busy)
        self.assertEqual(len(await self.plugin.tasks.list_for('test')), 1)

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
        dispatched = await self.execute('sandbox_worker')
        self.assertEqual(dispatched['status'], 'dispatched')
        await asyncio.wait_for(started.wait(), 1)
        waiting = asyncio.create_task(self.execute('note_write', note_path='notes/a.md'))
        self.addAsyncCleanup(self.cancel_task, waiting)
        ledger = AsyncMock(return_value='{"events":[]}')
        self.plugin.standalone_handlers['phone_events'] = ledger
        await self.execute('phone_events')
        note.assert_not_awaited()
        release.set()
        await self.wait_background(dispatched['task_id'])
        await asyncio.gather(waiting)
        note.assert_awaited_once()

    async def test_stop_cancels_a_background_task(self):
        started = asyncio.Event()

        async def run(request):
            started.set()
            await asyncio.Event().wait()

        self.plugin.standalone_handlers['phone_agent'] = run
        dispatched = await self.execute('phone_agent')
        await asyncio.wait_for(started.wait(), 1)
        reply = json.loads(await self.plugin.manage_tasks(
            self.run_context, 'stop', dispatched['task_id'],
        ))
        self.assertEqual(reply['status'], 'stopping')
        record = await self.wait_background(dispatched['task_id'])
        self.assertEqual(record['status'], 'stopped')
        self.assertFalse(self.plugin.phone_busy)

    async def test_status_and_result_tools_see_the_task(self):
        self.plugin.standalone_handlers['sandbox_worker'] = AsyncMock(
            return_value=json.dumps({
                'status': 'completed', 'summary': '写好了', 'command_count': 2,
                'device_action_count': 0, 'external_action_status': 'unknown',
                'raw_record': 'evidence',
            }),
        )
        dispatched = await self.execute('sandbox_worker')
        await self.wait_background(dispatched['task_id'])
        status = json.loads(await self.plugin.manage_tasks(self.run_context, 'status'))
        self.assertEqual([task['status'] for task in status['recent']], ['completed'])
        result = json.loads(await self.plugin.manage_tasks(
            self.run_context, 'result', dispatched['task_id'],
        ))
        self.assertEqual(result['result']['summary'], '写好了')
        self.assertEqual(result['result']['raw_record'], 'evidence')

    async def test_unknown_task_is_refused(self):
        reply = json.loads(await self.plugin.manage_tasks(self.run_context, 'stop', 'nope'))
        self.assertEqual(reply['reason'], 'unknown_task')
        reply = json.loads(await self.plugin.manage_tasks(self.run_context, 'explode'))
        self.assertEqual(reply['reason'], 'invalid_task_action')

    async def test_a_finished_task_does_not_talk_to_the_conversation(self):
        self.plugin.standalone_handlers['sandbox_worker'] = AsyncMock(
            return_value='{"status":"completed","summary":"好了"}',
        )
        dispatched = await self.execute('sandbox_worker')
        record = await self.wait_background(dispatched['task_id'])
        self.assertEqual(record['status'], 'completed')
        self.assertNotIn('delivery', record)
        self.assertFalse(hasattr(self.plugin.context, 'send_message'))

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
        self.plugin.standalone_handlers['note_read'] = AsyncMock(
            side_effect=RuntimeError('PRIVATE raw output'),
        )
        result = await self.execute('note_read', note_path='notes/a.md')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'execution_failed')
        self.assertNotIn('PRIVATE', json.dumps(result))

    async def test_background_failure_is_recorded_without_details(self):
        self.plugin.standalone_handlers['phone_agent'] = AsyncMock(
            side_effect=RuntimeError('PRIVATE raw output'),
        )
        dispatched = await self.execute('phone_agent')
        record = await self.wait_background(dispatched['task_id'])
        self.assertEqual(record['status'], 'failed')
        self.assertNotIn('PRIVATE', json.dumps(record))
        self.assertFalse(self.plugin.phone_busy)

    async def test_say_is_accepted_only_while_running(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def run(request):
            started.set()
            await release.wait()
            return '{"status":"completed","summary":"好了"}'

        self.plugin.standalone_handlers['phone_agent'] = run
        dispatched = await self.execute('phone_agent')
        await asyncio.wait_for(started.wait(), 1)
        reply = json.loads(await self.plugin.manage_tasks(
            self.run_context, 'say', dispatched['task_id'], '顺便看一眼电量',
        ))
        self.assertEqual(reply['status'], 'accepted')
        record = await self.plugin.tasks.get(dispatched['task_id'])
        self.assertEqual(record['followups'], ['顺便看一眼电量'])
        release.set()
        await self.wait_background(dispatched['task_id'])
        reply = json.loads(await self.plugin.manage_tasks(
            self.run_context, 'say', dispatched['task_id'], '来晚了',
        ))
        self.assertEqual(reply['reason'], 'task_not_running')

    async def test_say_requires_text(self):
        reply = json.loads(await self.plugin.manage_tasks(
            self.run_context, 'say', 'unknown', '   ',
        ))
        self.assertEqual(reply['reason'], 'unknown_task')

    async def test_finished_task_can_be_continued_from_saved_context(self):
        from operation_test.app.work_context import WorkContext

        seen = {}

        async def run(request):
            seen['resume'] = request.resume
            return '{"status":"completed","summary":"第二轮"}'

        self.plugin.standalone_handlers['phone_agent'] = run
        first = await self.execute('phone_agent')
        await self.wait_background(first['task_id'])
        work = WorkContext(self.plugin.data_dir / 'work_contexts', first['task_id'])
        work.save([{'role': 'user', 'content': '上一轮的记录'}])
        second = await self.execute('phone_agent', goal='接着做', task_id=first['task_id'])
        self.assertEqual(second['status'], 'dispatched')
        await self.wait_background(second['task_id'])
        self.assertEqual(seen['resume'], [{'role': 'user', 'content': '上一轮的记录'}])
        record = await self.plugin.tasks.get(second['task_id'])
        self.assertEqual(record['parent_task_id'], first['task_id'])

    async def test_continuation_needs_a_continuable_task(self):
        result = await self.execute('phone_agent', task_id='nope')
        self.assertEqual(result['reason'], 'unknown_task')
        result = await self.execute('phone_observe', task_id='nope')
        self.assertEqual(result['reason'], 'continuation_requires_background_capability')

    async def cancel_task(self, task):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
