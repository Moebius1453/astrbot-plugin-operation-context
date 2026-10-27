import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from .phone_actions import _TAP_SCRIPT, action_command, parse_receipt
from .phone_observation import PACKAGE


XML = (
    '<hierarchy>'
    '<node package="com.max.xiaoheihe" class="android.widget.RelativeLayout" resource-id="" text="" '
    'bounds="[0,873][720,965]" clickable="true" enabled="true">'
    '<node package="com.max.xiaoheihe" class="android.widget.TextView" '
    'resource-id="com.max.xiaoheihe:id/tv_desc" text="内容" bounds="[332,899][388,937]" '
    'clickable="false" enabled="true"/>'
    '</node></hierarchy>'
)
FOCUS = ' mCurrentFocus=Window{1 u0 com.max.xiaoheihe/.MainActivity}'


def fake_run(calls):
    def run(args, **kwargs):
        calls.append(args)
        command = args[3:]
        if command[:1] == ['shell'] and command[1:2] == ['dumpsys']:
            return SimpleNamespace(returncode=0, stdout=FOCUS, stderr='')
        if command[:1] == ['shell'] and command[1:2] == ['uiautomator']:
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if command[:1] == ['shell'] and command[1:2] == ['cat']:
            return SimpleNamespace(returncode=0, stdout=XML, stderr='')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    return run


class TapScriptTests(unittest.TestCase):
    def tap(self, target, match):
        payload = {'attr': 'resource-id' if match == 'id' else 'text', 'target': target}
        script = _TAP_SCRIPT.replace('PAYLOAD', repr(json.dumps(payload, ensure_ascii=False)), 1)
        calls = []
        output = io.StringIO()
        with patch('subprocess.run', fake_run(calls)), redirect_stdout(output):
            exec(script, {})
        return json.loads(output.getvalue()), calls

    def test_tap_uses_nearest_clickable_ancestor(self):
        result, calls = self.tap('内容', 'text')
        self.assertEqual(result['bounds'], [0, 873, 720, 965])
        tap_calls = [call for call in calls if call[3:5] == ['shell', 'input']]
        self.assertEqual(len(tap_calls), 1)
        self.assertEqual(tap_calls[0][-2:], ['360', '919'])

    def test_tap_by_text_matches_content_desc(self):
        script = _TAP_SCRIPT.replace('PAYLOAD', repr(json.dumps({'attr': 'text', 'target': '搜索'}, ensure_ascii=False)), 1)
        xml = ('<hierarchy><node package="com.max.xiaoheihe" class="android.widget.FrameLayout" text="" '
               'content-desc="搜索" bounds="[0,0][80,80]" clickable="true" enabled="true"/></hierarchy>')
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            command = args[3:]
            if command[:2] == ['shell', 'dumpsys']:
                return SimpleNamespace(returncode=0, stdout=FOCUS, stderr='')
            if command[:2] == ['shell', 'uiautomator']:
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            if command[:2] == ['shell', 'cat']:
                return SimpleNamespace(returncode=0, stdout=xml, stderr='')
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        output = io.StringIO()
        with patch('subprocess.run', run), redirect_stdout(output):
            exec(script, {})
        self.assertEqual(json.loads(output.getvalue())['bounds'], [0, 0, 80, 80])

    def test_tap_reports_not_interactive_without_clickable_ancestor(self):
        script = _TAP_SCRIPT.replace('PAYLOAD', repr(json.dumps({'attr': 'resource-id', 'target': 'missing'})), 1)
        with patch('subprocess.run', fake_run([])), self.assertRaisesRegex(RuntimeError, 'missing_or_ambiguous_target'):
            exec(script, {})


class ActionCommandTests(unittest.TestCase):
    def test_tap_normalizes_resource_id_and_match(self):
        command = action_command('phone_tap', {'target': 'et_editor'}, {})
        self.assertIn(PACKAGE + ':id/et_editor', command)
        text_command = action_command('phone_tap', {'target': '发布', 'match': 'text'}, {})
        self.assertIn('发布', text_command)
        self.assertIn('"attr": "text"', text_command)
        self.assertIn('content-desc', text_command)

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
