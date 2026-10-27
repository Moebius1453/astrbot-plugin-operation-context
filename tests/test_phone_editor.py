import unittest
from unittest.mock import AsyncMock, patch

from ..app.phone_editor import EDITORS, POST_ACTIVITY, input_command, prepare_test_post
from ..app.phone_observation import PhoneNode, PhoneObservation
from ..app.publication import Publication


TEXT = '【测试】测试 2026-10-27 16:30:00'


def observation(title='填写标题', content='添加正文'):
    return PhoneObservation(POST_ACTIVITY, 'uiautomator', tuple(
        PhoneNode(EDITORS[field], 'android.widget.EditText', text, (32, 156, 659, 212), True, True)
        for field, text in [('title', title), ('content', content)]
    ))


class EditorTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_exact_text_with_readback(self):
        execute = AsyncMock(return_value={'exit_code': 0, 'stdout': '{"input_attempted":true,"publication_attempted":false}'})
        with patch('operation_test.app.phone_editor.observe_phone', AsyncMock(side_effect=[observation(), observation(TEXT), observation(TEXT, TEXT), observation(TEXT, TEXT)])):
            result = await prepare_test_post(Publication('post', 'own_account', TEXT, TEXT), execute)
        self.assertEqual(result, {'target': 'own_account', 'title': TEXT, 'text': TEXT})
        self.assertEqual(execute.await_count, 2)

    async def test_existing_body_protects_title_too(self):
        execute = AsyncMock()
        with patch('operation_test.app.phone_editor.observe_phone', AsyncMock(return_value=observation(content='existing draft'))):
            with self.assertRaisesRegex(ValueError, 'existing_draft'):
                await prepare_test_post(Publication('post', 'own_account', TEXT, TEXT), execute)
        execute.assert_not_called()

    async def test_input_failure_is_not_retried(self):
        execute = AsyncMock(return_value={'exit_code': 1})
        with patch('operation_test.app.phone_editor.observe_phone', AsyncMock(return_value=observation())):
            with self.assertRaisesRegex(ValueError, 'input_failed_no_retry'):
                await prepare_test_post(Publication('post', 'own_account', TEXT, TEXT), execute)
        self.assertEqual(execute.await_count, 1)

    async def test_readback_mismatch_stops_before_second_input(self):
        execute = AsyncMock(return_value={'exit_code': 0, 'stdout': '{"input_attempted":true,"publication_attempted":false}'})
        with patch('operation_test.app.phone_editor.observe_phone', AsyncMock(side_effect=[observation(), observation('wrong')])):
            with self.assertRaisesRegex(ValueError, 'editor_text_not_verified'):
                await prepare_test_post(Publication('post', 'own_account', TEXT, TEXT), execute)
        self.assertEqual(execute.await_count, 1)

    def test_only_test_text_and_empty_fields_allowed(self):
        with self.assertRaises(ValueError):
            input_command('content', 'existing draft', TEXT)
        with self.assertRaises(ValueError):
            input_command('content', '', 'invented')
