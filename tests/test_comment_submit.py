import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from ..app.comment_submit import CommentSubmitter, comment_command, validate_comment


class ValidateTests(unittest.TestCase):
    def test_accepts_normal_text(self):
        validate_comment('发出来了，你验收一下。')

    def test_rejects_empty_long_and_control_characters(self):
        for text in ('', '   ', None, 'x' * 1001, 'a\x07b'):
            with self.assertRaises(ValueError):
                validate_comment(text)


class CommandTests(unittest.TestCase):
    def test_command_checks_content_before_sending(self):
        command = comment_command('原文')
        self.assertIn('et_editor', command)
        self.assertIn('comment_content_mismatch', command)
        self.assertIn('send_control_missing', command)
        self.assertLess(command.index('comment_content_mismatch'),
                        command.index('submit_tapped'))

    def test_command_is_quoted(self):
        command = comment_command("it's fine")
        self.assertTrue(command.startswith('python3 -I -c '))


class SubmitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    async def submit(self, envelope):
        async def execute(command):
            return envelope
        return await CommentSubmitter(execute, self.root).submit('评论原文')

    async def test_confirmed_receipt(self):
        result = await self.submit({'exit_code': 0, 'stdout': json.dumps(
            {'submit_tapped': True, 'confirmed': True})})
        self.assertEqual(result['status'], 'confirmed')
        self.assertTrue((self.root / result['request'] / 'attempt.json').exists())

    async def test_readback_failure_stays_unknown(self):
        result = await self.submit({'exit_code': 0, 'stdout': json.dumps(
            {'submit_tapped': True, 'confirmed': False})})
        self.assertEqual(result['status'], 'unknown')

    async def test_command_failure_stays_unknown_without_retry(self):
        calls = []

        async def execute(command):
            calls.append(command)
            return {'exit_code': 1, 'stderr': 'RuntimeError: comment_content_mismatch'}

        result = await CommentSubmitter(execute, self.root).submit('评论原文')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['error_type'], 'submit_command_failed')
        self.assertIn('comment_content_mismatch', result['stderr'])
        self.assertEqual(len(calls), 1)

    async def test_malformed_receipt_stays_unknown(self):
        result = await self.submit({'exit_code': 0, 'stdout': 'not json'})
        self.assertEqual(result['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
