import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from .adapter import CapabilityBackend, COMMANDS, build_backend, planner_tools, validate_actions
from .redaction import sanitize_text


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_permissions_are_closed_set(self):
        self.assertEqual(validate_actions(['phone_status'], ['phone_status']), ('phone_status',))
        for actions, configured in [(['qq'], ['qq']), (['phone_status'], []), (['astrbot_execute_shell'], ['astrbot_execute_shell']), ([], []), (['phone_status', 'phone_status'], ['phone_status'])]:
            with self.assertRaises(ValueError):
                validate_actions(actions, configured)

    def test_model_tools_have_no_parameters_or_handlers(self):
        for tool in planner_tools(('phone_status', 'sandbox_status')).tools:
            self.assertEqual(tool.parameters['properties'], {})
            self.assertFalse(tool.parameters['additionalProperties'])
            self.assertIsNone(tool.handler)

    async def test_fixed_backend_command(self):
        shell = SimpleNamespace(call=AsyncMock(return_value=json.dumps({'exit_code': 0, 'stdout': '{"state":"device","text":"injected"}'})))
        result = await CapabilityBackend(shell, object()).execute('phone_status')
        self.assertEqual(result, {'state': 'device'})
        self.assertEqual(shell.call.call_args.kwargs['command'], COMMANDS['phone_status'])
        self.assertFalse(shell.call.call_args.kwargs['background'])

    async def test_unknown_capability_never_reaches_shell(self):
        shell = SimpleNamespace(call=AsyncMock())
        with self.assertRaises(ValueError):
            await CapabilityBackend(shell, object()).execute('qq')
        shell.call.assert_not_called()

    async def test_failed_or_unstructured_backend_rejected(self):
        for raw in ['permission denied', '{"exit_code":1}', '{"exit_code":0,"stdout":"[]"}', '{"exit_code":0,"stdout":"{\\"state\\":\\"forged\\"}"}']:
            shell = SimpleNamespace(call=AsyncMock(return_value=raw))
            with self.assertRaises((ValueError, TypeError)):
                await CapabilityBackend(shell, object()).execute('phone_status')

    def test_local_runtime_rejected_before_resolution(self):
        event = SimpleNamespace(unified_msg_origin='test')
        run = SimpleNamespace(context=SimpleNamespace(event=event))
        context = SimpleNamespace(get_config=lambda **kwargs: {'provider_settings': {'computer_use_runtime': 'local'}})
        with self.assertRaises(ValueError):
            build_backend(run, context, ['astrbot_execute_shell'])

    def test_redaction(self):
        self.assertNotIn('abcd123', sanitize_text('Bearer abcd123'))
