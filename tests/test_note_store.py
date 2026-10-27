import base64
import json
import unittest

from ..app.note_store import (
    NoteStore, validate_content, validate_mode, validate_note_path,
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


if __name__ == '__main__':
    unittest.main()
