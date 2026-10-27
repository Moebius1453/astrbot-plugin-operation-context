import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import contextlib
import io

from .adapter import (
    _APP_SCOPE_SCRIPT,
    CAPABILITIES,
    CapabilityBackend,
    COMMANDS,
    build_backend,
    planner_tools,
    validate_actions,
)
from .redaction import sanitize_text


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_capability_set_is_narrow(self):
        self.assertEqual(CAPABILITIES, {'sandbox_status', 'phone_status', 'phone_app_scope'})

    def test_permissions_are_closed_set(self):
        self.assertEqual(validate_actions(['phone_status'], ['phone_status']), ('phone_status',))
        for actions, configured in [
            (['qq'], ['qq']),
            (['phone_status'], []),
            (['astrbot_execute_shell'], ['astrbot_execute_shell']),
            ([], []),
            (['phone_status', 'phone_status'], ['phone_status']),
        ]:
            with self.assertRaises(ValueError):
                validate_actions(actions, configured)

    def test_model_tools_have_empty_parameter_schema(self):
        for tool in planner_tools(('phone_status', 'phone_app_scope')).tools:
            self.assertEqual(tool.parameters['properties'], {})
            self.assertFalse(tool.parameters['additionalProperties'])
            self.assertIsNone(tool.handler)

    async def test_app_scope_filters_private_fields(self):
        for scope in ('allowed_app', 'blocked_or_unknown'):
            shell = SimpleNamespace(call=AsyncMock(return_value=json.dumps({
                'exit_code': 0,
                'stdout': json.dumps({'scope': scope, 'private_text': 'must-not-return'}),
            })))
            result = await CapabilityBackend(shell, None).execute('phone_app_scope')
            self.assertEqual(result, {'scope': scope, 'content_read': False})
            self.assertEqual(shell.call.call_args.kwargs['command'], COMMANDS['phone_app_scope'])

    def test_scope_script_is_read_only_and_fail_closed(self):
        self.assertIn("'dumpsys', 'activity', 'activities'", _APP_SCOPE_SCRIPT)
        self.assertIn("'com.max.xiaoheihe'", _APP_SCOPE_SCRIPT)
        for forbidden in ('input tap', 'input text', 'KEYCODE_', 'screencap', 'uiautomator dump'):
            self.assertNotIn(forbidden, _APP_SCOPE_SCRIPT)
        self.assertIn("'blocked_or_unknown'", _APP_SCOPE_SCRIPT)

    async def test_fixed_phone_backend_command(self):
        shell = SimpleNamespace(call=AsyncMock(return_value=json.dumps({
            'exit_code': 0,
            'stdout': '{"state":"device","text":"must-not-return"}',
        })))
        result = await CapabilityBackend(shell, object()).execute('phone_status')
        self.assertEqual(result, {'state': 'device'})
        self.assertEqual(shell.call.call_args.kwargs['command'], COMMANDS['phone_status'])
        self.assertFalse(shell.call.call_args.kwargs['background'])

    async def test_unknown_capability_never_reaches_shell(self):
        shell = SimpleNamespace(call=AsyncMock())
        with self.assertRaises(ValueError):
            await CapabilityBackend(shell, object()).execute('qq')
        shell.call.assert_not_called()

    async def test_bad_backend_results_are_rejected(self):
        for action, raw in [
            ('phone_status', 'permission denied'),
            ('phone_status', '{"exit_code":1}'),
            ('phone_status', '{"exit_code":0,"stdout":"[]"}'),
            ('phone_status', '{"exit_code":0,"stdout":"{\\"state\\":\\"forged\\"}"}'),
            ('phone_app_scope', json.dumps({'exit_code': 0, 'stdout': '{"scope":"qq"}'})),
        ]:
            shell = SimpleNamespace(call=AsyncMock(return_value=raw))
            with self.assertRaises((ValueError, TypeError)):
                await CapabilityBackend(shell, object()).execute(action)

    def test_bay_command_uses_exact_device_and_read_only_scope(self):
        self.assertIn("'-s', '172.22.0.2:5555'", _APP_SCOPE_SCRIPT)
        self.assertNotIn('send_message_to_user', COMMANDS['phone_app_scope'])

    def test_android14_foreground_markers(self):
        for marker in ('mResumedActivity:', 'topResumedActivity=', 'ResumedActivity:'):
            for package, expected in (
                ('com.max.xiaoheihe', 'allowed_app'),
                ('com.tencent.mobileqq', 'blocked_or_unknown'),
                ('com.max.xiaoheihe.fake', 'blocked_or_unknown'),
            ):
                output = io.StringIO()
                text = f'{marker} ActivityRecord{{123 u0 {package}/.Main t1}}'
                with patch('os.path.isfile', return_value=True), patch(
                    'subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=text)
                ), contextlib.redirect_stdout(output):
                    exec(_APP_SCOPE_SCRIPT, {})
                self.assertEqual(json.loads(output.getvalue()), {'scope': expected})

    def test_local_runtime_rejected_before_resolution(self):
        context = SimpleNamespace(get_config=lambda **kwargs: {
            'provider_settings': {'computer_use_runtime': 'local'}
        })
        run_context = SimpleNamespace(context=SimpleNamespace(event=SimpleNamespace(unified_msg_origin='test')))
        with patch('astrbot.core.astr_agent_tool_exec.FunctionToolExecutor._build_handoff_toolset') as resolve:
            with self.assertRaisesRegex(ValueError, 'sandbox_runtime_required'):
                build_backend(run_context, context, ['astrbot_execute_shell'])
            resolve.assert_not_called()

    def test_redaction(self):
        self.assertNotIn('abcd123', sanitize_text('Bearer abcd123'))
