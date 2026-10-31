import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from ..app.task_view import refresh_task_receipts
from ..app.artifacts import Artifacts, ArtifactTool


class TaskViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_refresh_changes_only_matching_tool_result(self):
        task_id = 'a' * 32
        store = AsyncMock()
        store.get.return_value = {'umo': 'u', 'conversation_id': 'c', 'status': 'completed',
                                  'summary': '已写入', 'result': {'raw_record': 'evidence'}}
        original = [{'role': 'user', 'content': '去玩'},
                    {'role': 'tool', 'tool_call_id': 'call-1', 'content': json.dumps({
                        'status': 'dispatched', 'task_id': task_id})}]
        result = await refresh_task_receipts(original, store, 'u', 'c')
        self.assertEqual(result[0], original[0])
        self.assertNotIn('current_task', original[1]['content'])
        self.assertEqual(result[1]['tool_call_id'], 'call-1')
        self.assertEqual(json.loads(result[1]['content'])['current_task']['summary'], '已写入')

    async def test_foreign_and_reset_conversation_are_not_visible(self):
        store = AsyncMock()
        store.get.return_value = {'umo': 'u', 'conversation_id': 'old'}
        original = [{'role': 'tool', 'content': json.dumps({
            'status': 'dispatched', 'task_id': 'a' * 32})}]
        self.assertEqual(await refresh_task_receipts(original, store, 'u', 'new'), original)
        self.assertEqual(await refresh_task_receipts(original, store, 'other', 'old'), original)

    async def test_invalid_ids_are_not_read(self):
        store = AsyncMock()
        original = [{'role': 'tool', 'content': json.dumps({
            'status': 'dispatched', 'task_id': '../../secret'})}]
        self.assertEqual(await refresh_task_receipts(original, store, 'u', 'c'), original)
        store.get.assert_not_called()


class PluginSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from ..main import OperationContextPlugin
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.context = SimpleNamespace(
            add_llm_tools=Mock(), register_web_api=Mock(),
            conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='c')),
        )
        with patch('operation_test.main.StarTools.get_data_dir', return_value=Path(self.temp.name)):
            self.plugin = OperationContextPlugin(self.context, {})
        self.event = SimpleNamespace(
            unified_msg_origin='u', is_admin=lambda: True,
            get_extra=lambda key: ['astrbot_execute_shell'], send=AsyncMock(),
            chain_result=lambda chain: chain,
        )
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=self.event), messages=[])

    async def test_capture_view_send_and_busy(self):
        import io
        from PIL import Image
        from unittest.mock import patch
        output = io.BytesIO()
        Image.new('RGB', (2, 2)).save(output, format='PNG')
        with patch('operation_test.main.build_backend', return_value=object()), patch(
            'operation_test.main.capture', AsyncMock(return_value=output.getvalue()),
        ) as grab:
            self.plugin.phone_busy = True
            busy = json.loads(await self.plugin.manage_image(self.run_context, 'capture', 'phone'))
            self.assertEqual(busy['status'], 'busy')
            grab.assert_not_awaited()
            self.plugin.phone_busy = False
            result = await self.plugin.manage_image(self.run_context, 'capture', 'phone')
            self.assertFalse(self.plugin.phone_busy)
            self.assertEqual(self.run_context.messages[-1].content[0].type, 'image_url')
            artifact_id = json.loads(result.content[0].text)['artifact_id']
            self.event.send.assert_not_awaited()
            viewed = await self.plugin.manage_image(self.run_context, 'view', artifact_id=artifact_id)
            self.assertEqual(len(self.run_context.messages), 2)
            self.assertTrue(json.loads(viewed.content[0].text)['image_attached'])
            sent = json.loads(await self.plugin.manage_image(self.run_context, 'send', artifact_id=artifact_id))
            self.assertEqual(sent['status'], 'sent')
            self.event.send.assert_awaited_once()

    async def test_capture_failure_releases_phone(self):
        from unittest.mock import patch
        with patch('operation_test.main.build_backend', return_value=object()), patch(
            'operation_test.main.capture', AsyncMock(side_effect=ValueError('capture_failed')),
        ):
            result = json.loads(await self.plugin.manage_image(self.run_context, 'capture', 'phone'))
        self.assertEqual(result['reason'], 'capture_failed')
        self.assertFalse(self.plugin.phone_busy)

    async def test_hook_only_refreshes_facts_and_does_not_generate(self):
        from types import SimpleNamespace
        record = await self.plugin.tasks.create(umo='u', conversation_id='c', kind='sandbox_worker', description='写文件')
        await self.plugin.tasks.update(record['task_id'], status='completed', summary='写入完成')
        req = SimpleNamespace(func_tool=None, conversation=SimpleNamespace(cid='c'),
                              contexts=[{'role': 'tool', 'content': json.dumps({
                                  'status': 'dispatched', 'task_id': record['task_id']})}],
                              prompt='你好', system_prompt='原系统提示词')
        self.event.set_extra = lambda key, value: None
        await self.plugin.capture_authorized_tools(self.event, req)
        self.assertEqual(req.prompt, '你好')
        self.assertEqual(req.system_prompt, '原系统提示词')
        self.assertEqual(len(req.contexts), 1)
        self.assertEqual(json.loads(req.contexts[0]['content'])['current_task']['summary'], '写入完成')

    async def test_executor_saves_artifact_without_receiving_image(self):
        from ..app.subagent import SandboxAgent
        from .test_subagent import Planner, Response, Shell, done
        capture = AsyncMock(return_value='a' * 32)
        with tempfile.TemporaryDirectory() as directory:
            result = await SandboxAgent(Shell(), None, Path(directory)).run(
                context=Planner([Response(['capture_artifact'], [{}]), done('截图已保存')]),
                provider_id='mock', goal='截图', capture_artifact=capture,
            )
        self.assertEqual(result['artifacts'], ['a' * 32])
        self.assertEqual(result['command_count'], 0)
        capture.assert_awaited_once()

    async def test_web_routes_list_detail_and_reject_traversal(self):
        from quart import Quart
        await self.plugin.initialize()
        self.assertEqual(self.context.register_web_api.call_count, 3)
        record = await self.plugin.tasks.create(umo='u', conversation_id='c', kind='phone_agent', description='查页面')
        app = Quart(__name__)
        app.add_url_rule('/tasks', view_func=self.plugin.web_tasks, methods=['GET'])
        app.add_url_rule('/detail', view_func=self.plugin.web_detail, methods=['POST'])
        app.add_url_rule('/control', view_func=self.plugin.web_control, methods=['POST'])
        async with app.test_client() as client:
            response = await client.get('/tasks')
            self.assertEqual(len((await response.get_json())['tasks']), 1)
            response = await client.post('/detail', json={'task_id': record['task_id']})
            self.assertEqual(response.status_code, 200)
            response = await client.post('/detail', json={'task_id': '../../secret'})
            self.assertEqual(response.status_code, 400)
            await self.plugin.tasks.update(record['task_id'], status='completed')
            response = await client.post('/control', json={'task_id': record['task_id'], 'action': 'stop'})
            self.assertEqual(response.status_code, 409)


class ArtifactTests(unittest.TestCase):
    def test_png_roundtrip_and_scope(self):
        import io
        from PIL import Image
        output = io.BytesIO()
        Image.new('RGB', (2, 2)).save(output, format='PNG')
        with tempfile.TemporaryDirectory() as directory:
            store = Artifacts(Path(directory))
            artifact_id = store.save_png(output.getvalue(), 'u', 'c', 'phone')
            path = store.get(artifact_id, 'u', 'c')
            from types import SimpleNamespace
            run = SimpleNamespace(messages=[])
            result = ArtifactTool.image_result(artifact_id, path, run)
            self.assertEqual(run.messages[0].content[0].type, 'image_url')
            self.assertEqual(len(result.content), 1)
            self.assertTrue(json.loads(result.content[0].text)['image_attached'])
            for umo, cid in [('other', 'c'), ('u', 'new')]:
                with self.assertRaises(ValueError):
                    store.get(artifact_id, umo, cid)
            with self.assertRaises(ValueError):
                store.get('../secret', 'u', 'c')

    def test_rejects_non_image(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises((ValueError, OSError)):
                Artifacts(Path(directory)).save_png(b'not image', 'u', 'c', 'phone')
