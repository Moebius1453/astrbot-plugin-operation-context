import unittest

from .phone_actions import action_command, parse_receipt
from .phone_observation import PACKAGE


class ActionCommandTests(unittest.TestCase):
    def test_tap_normalizes_resource_id_and_match(self):
        command = action_command('phone_tap', {'target': 'et_editor'}, {})
        self.assertIn(PACKAGE + ':id/et_editor', command)
        text_command = action_command('phone_tap', {'target': '发布', 'match': 'text'}, {})
        self.assertIn('发布', text_command)
        self.assertNotIn('resource-id', text_command)

    def test_input_embeds_only_frozen_text(self):
        command = action_command('phone_input', {'target': 'et_editor'}, {'text': '冻结原文'})
        self.assertIn('冻结原文', command)
        self.assertIn('ADB_INPUT_B64', command)
        with self.assertRaises(KeyError):
            action_command('phone_input', {'target': 'et_editor'}, {})

    def test_resource_id_rejects_path_and_quotes(self):
        for target in ('../../etc/passwd', 'et editor', "a';rm -rf /"):
            with self.assertRaises(ValueError):
                action_command('phone_tap', {'target': target}, {})

    def test_scripts_guard_foreground_device_and_bounds(self):
        for action, arguments in [
            ('phone_tap', {'target': 'et_editor'}),
            ('phone_input', {'target': 'et_editor'}),
            ('phone_swipe', {'target': 'rv', 'direction': 'up'}),
            ('phone_back', {}),
            ('phone_start_app', {}),
        ]:
            command = action_command(action, arguments, {'text': 'x'})
            self.assertIn('172.22.0.2:5555', command)
            self.assertNotIn('send_message_to_user', command)
        for action, arguments in [
            ('phone_tap', {'target': 'et_editor'}),
            ('phone_input', {'target': 'et_editor'}),
            ('phone_swipe', {'target': 'rv', 'direction': 'up'}),
            ('phone_back', {}),
        ]:
            self.assertIn('phone_outside_allowed_app', action_command(action, arguments, {'text': 'x'}))

    def test_swipe_stays_inside_target_bounds(self):
        command = action_command('phone_swipe', {'target': 'rv', 'direction': 'up'}, {})
        self.assertIn('swipe', command)
        self.assertIn('unsafe_target_bounds', command)
        self.assertIn('"direction": "up"', command)

    def test_screenshot_only_writes_workspace_png(self):
        command = action_command('phone_screenshot', {}, {})
        self.assertIn('/workspace/phone-shot-', command)
        self.assertIn('screencap', command)
        self.assertIn('\\x89PNG', command)


class ReceiptTests(unittest.TestCase):
    def test_tap_receipt_requires_attempt_and_bounds(self):
        self.assertEqual(parse_receipt('phone_tap', {'tap_attempted': True, 'bounds': [1, 2, 3, 4]}),
                         {'action': 'tap', 'bounds': [1, 2, 3, 4]})
        for data in ({'tap_attempted': False, 'bounds': [1, 2, 3, 4]}, {'tap_attempted': True},
                     {'tap_attempted': True, 'bounds': ['1', 2, 3, 4]}):
            with self.assertRaises(ValueError):
                parse_receipt('phone_tap', data)

    def test_input_receipt_separates_applied_from_unknown(self):
        self.assertEqual(parse_receipt('phone_input', {'input_attempted': True, 'text_verified': True}),
                         {'action': 'input', 'state': 'applied', 'text_verified': True})
        for verified in (False, None):
            self.assertEqual(parse_receipt('phone_input', {'input_attempted': True, 'text_verified': verified})['state'],
                             'unknown')
        with self.assertRaises(ValueError):
            parse_receipt('phone_input', {'input_attempted': False, 'text_verified': True})

    def test_back_and_start_receipts(self):
        self.assertEqual(parse_receipt('phone_back', {'back_attempted': True}), {'action': 'back'})
        self.assertEqual(parse_receipt('phone_start_app', {'start_attempted': True})['action'], 'start_app')
        with self.assertRaises(ValueError):
            parse_receipt('phone_back', {'back_attempted': False})

    def test_swipe_home_and_screenshot_receipts(self):
        self.assertEqual(parse_receipt('phone_swipe', {'swipe_attempted': True, 'bounds': [0, 0, 10, 10]}),
                         {'action': 'swipe', 'bounds': [0, 0, 10, 10]})
        self.assertEqual(parse_receipt('phone_home', {'home_attempted': True}), {'action': 'home'})
        path = '/workspace/phone-shot-' + 'a' * 32 + '.png'
        self.assertEqual(parse_receipt('phone_screenshot', {'screenshot': path, 'bytes': 12}),
                         {'action': 'screenshot', 'path': path, 'bytes': 12})
        for data in ({'screenshot': '/workspace/other.png', 'bytes': 12},
                     {'screenshot': path, 'bytes': 0}):
            with self.assertRaises(ValueError):
                parse_receipt('phone_screenshot', data)
