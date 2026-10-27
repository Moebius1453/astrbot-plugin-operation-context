import json
import tempfile
import unittest
from pathlib import Path

from ..app.post_submit import PostSubmitter, submit_command, validate_post


class PostPayloadTests(unittest.TestCase):
    def test_accepts_normal_title_and_body(self):
        validate_post('今天的记录', '第一条正文\n第二行')
        validate_post('a', 'b')

    def test_rejects_empty_overlong_and_control_characters(self):
        for title, text in [('', '正文'), ('标题', '   '), ('x' * 101, '正文'),
                            ('标题', 'y' * 4001), ('标题\x00', '正文'), ('标题', '正文\x07')]:
            with self.assertRaises(ValueError):
                validate_post(title, text)

    def test_command_embeds_frozen_text_and_guards_editor(self):
        command = submit_command('标题', '正文')
        self.assertIn('标题', command)
        self.assertIn('正文', command)
        self.assertIn('editor_required', command)
        self.assertIn('editor_content_mismatch', command)
        self.assertIn('post_type_not_normal', command)
        self.assertIn('tv_appbar_title', command)
        self.assertIn('直接发布', command)
        self.assertIn('172.22.0.2:5555', command)
        self.assertNotIn('send_message_to_user', command)


class PostSubmitterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    async def test_attempt_is_written_before_submit(self):
        seen = {}

        async def execute(command):
            attempts = list(self.root.glob('*/attempt.json'))
            seen['attempt'] = json.loads(attempts[0].read_text(encoding='utf-8')) if attempts else None
            return {'exit_code': 0, 'stdout': json.dumps({
                'submit_tapped': True, 'dialog_tapped': True, 'activity': 'a.PicturePostPageActivityV2',
                'confirmed': True,
            })}

        result = await PostSubmitter(execute, self.root).submit('标题', '正文')
        self.assertEqual(seen['attempt']['status'], 'unknown')
        self.assertEqual(seen['attempt']['title'], '标题')
        self.assertEqual(result['status'], 'confirmed')
        self.assertFalse(result['object_id_verified'])

    async def test_unconfirmed_is_unknown_and_not_retried(self):
        calls = []

        async def execute(command):
            calls.append(command)
            return {'exit_code': 0, 'stdout': json.dumps({
                'submit_tapped': True, 'dialog_tapped': False, 'activity': 'a.MainActivity', 'confirmed': False,
            })}

        submitter = PostSubmitter(execute, self.root)
        result = await submitter.submit('标题', '正文')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(len(calls), 1)

    async def test_command_failure_is_unknown(self):
        async def execute(command):
            return {'exit_code': 1, 'stderr': 'RuntimeError: editor_content_mismatch'}

        result = await PostSubmitter(execute, self.root).submit('标题', '正文')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['error_type'], 'submit_command_failed')
        self.assertIn('editor_content_mismatch', result['stderr'])

    async def test_exception_is_recorded_as_unknown(self):
        async def execute(command):
            raise TimeoutError()

        result = await PostSubmitter(execute, self.root).submit('标题', '正文')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['error_type'], 'TimeoutError')
        self.assertEqual(len(list(self.root.glob('*/result.json'))), 1)
