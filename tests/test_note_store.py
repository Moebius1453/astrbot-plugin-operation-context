import asyncio
import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..app.note_store import (
    NoteStore, WorkspaceAccess, _WRITE_SCRIPT, validate_content, validate_mode, validate_note_path,
)


class PathTests(unittest.TestCase):
    def test_accepts_notes_and_diary(self):
        self.assertEqual(validate_note_path('notes/工具用法.md'), '/workspace/shiro/notes/工具用法.md')
        self.assertEqual(
            validate_note_path('/diary/2026-10/2026-10-27.md'),
            '/workspace/shiro/diary/2026-10/2026-10-27.md',
        )

    def test_rejects_escapes_and_other_directories(self):
        for path in (
            '../etc/passwd.md', 'notes/../../x.md', '/workspace/shiro/notes/a.md',
            'tools/a.md', 'notes/.md', 'notes/a.txt', 'notes', '', None, 3,
            'notes/a..b.md/..', 'notes//a.md',
        ):
            with self.assertRaises(ValueError):
                validate_note_path(path)

    def test_mode_and_content_are_closed(self):
        self.assertEqual(validate_mode('append'), 'append')
        for mode in ('write', '', None):
            with self.assertRaises(ValueError):
                validate_mode(mode)
        for text in ('', '   ', None, 'x' * 20001):
            with self.assertRaises(ValueError):
                validate_content(text)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_decodes_content(self):
        content = '# 标题\n正文'

        async def execute(command):
            self.assertIn('note_missing', command)
            return {'exit_code': 0, 'stdout': json.dumps({
                'content': base64.b64encode(content.encode()).decode(), 'bytes': len(content.encode()),
            })}

        result = await NoteStore(execute).read('notes/工具用法.md')
        self.assertEqual(result['content'], content)
        self.assertFalse(result['truncated'])

    async def test_read_missing_file_raises(self):
        async def execute(command):
            return {'exit_code': 1, 'stderr': 'RuntimeError: note_missing'}

        with self.assertRaisesRegex(ValueError, 'note_read_failed'):
            await NoteStore(execute).read('notes/不存在.md')

    async def test_write_passes_mode_and_content(self):
        seen = {}

        async def execute(command):
            seen['command'] = command
            return {'exit_code': 0, 'stdout': json.dumps({'written': True, 'bytes': 6, 'backup': None})}

        result = await NoteStore(execute).write('notes/工具用法.md', 'append', '新内容')
        self.assertIs(result['written'], True)
        self.assertIn('note_already_exists', seen['command'])
        self.assertIn('bak-', seen['command'])
        self.assertIn('append', seen['command'])

    async def test_write_failure_raises(self):
        async def execute(command):
            return {'exit_code': 1, 'stderr': 'RuntimeError: note_already_exists'}

        with self.assertRaisesRegex(ValueError, 'note_write_failed'):
            await NoteStore(execute).write('notes/a.md', 'create', '内容')


    def test_same_second_replacements_keep_each_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'notes' / 'a.md'
            path.parent.mkdir()
            path.write_text('初稿', encoding='utf-8')
            backups = []
            for content in ('第二稿', '第三稿'):
                payload = json.dumps({
                    'path': str(path), 'mode': 'replace',
                    'content': base64.b64encode(content.encode()).decode(),
                })
                script = _WRITE_SCRIPT.replace('PAYLOAD', repr(payload), 1)
                output = io.StringIO()
                with patch('os.path.abspath', return_value='/workspace/shiro/notes'), patch(
                    'time.strftime', return_value='20261029-010000',
                ), contextlib.redirect_stdout(output):
                    exec(script, {})
                backups.append(Path(json.loads(output.getvalue())['backup']))
            self.assertNotEqual(*backups)
            self.assertEqual([backup.read_text(encoding='utf-8') for backup in backups], ['初稿', '第二稿'])
            self.assertEqual(path.read_text(encoding='utf-8'), '第三稿')


class WorkspaceAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_waits_for_notes_and_holds_workspace(self):
        access = WorkspaceAccess()
        attempted, started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def worker():
            attempted.set()
            async with access.worker():
                started.set()
                await release.wait()

        async with access.note('notes/a.md'):
            task = asyncio.create_task(worker())
            try:
                await asyncio.wait_for(attempted.wait(), 1)
                self.assertFalse(started.is_set())
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        await asyncio.wait_for(started.wait(), 1)
        self.assertTrue(access._worker_active)
        release.set()
        await task
        self.assertFalse(access._worker_active)

    async def test_cancelled_waiter_does_not_block_later_notes(self):
        access = WorkspaceAccess()
        attempted = asyncio.Event()

        async def worker():
            attempted.set()
            async with access.worker():
                self.fail('The workspace should still be in use')

        async with access.note('notes/a.md'):
            task = asyncio.create_task(worker())
            await asyncio.wait_for(attempted.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            async with asyncio.timeout(1):
                async with access.note('notes/b.md'):
                    self.assertEqual(access._waiting_workers, 0)
        self.assertEqual(access._paths, set())


if __name__ == '__main__':
    unittest.main()
