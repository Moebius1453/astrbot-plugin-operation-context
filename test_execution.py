import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from .execution import EXECUTION_PROTOCOL, IndependentExecution


class Planner:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        try:
            return next(self.responses)
        except StopIteration:
            return response([])


def response(names=(), args=None, text=''):
    return SimpleNamespace(tools_call_name=list(names), tools_call_args=args if args is not None else [{} for _ in names], completion_text=text)


class Backend:
    def __init__(self):
        self.calls = []

    async def execute(self, name, args=None):
        self.calls.append((name, args))
        if name == 'phone_observe':
            return {'activity': 'a', 'source': 'uiautomator', 'text_available': True, 'node_count': 2,
                    'truncated': False, 'nodes': [{'id': 'x', 'text': '目标文字'}]}
        return {'state': 'device'}


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.executor = IndependentExecution(Path(self.directory.name), lambda text: text)
        self.backend = Backend()

    async def run_task(self, planner, actions=('phone_status',), **kwargs):
        return await self.executor.run(context=planner, provider_id='fake', goal='status', actions=actions, backend=self.backend, **kwargs)

    async def test_observation_without_final_text(self):
        planner = Planner([response(['phone_status'])])
        result = await self.run_task(planner)
        self.assertEqual(result['status'], 'observations_returned')
        self.assertEqual(self.backend.calls, [('phone_status', {})])
        request = planner.requests[0]
        self.assertEqual(request['system_prompt'], EXECUTION_PROTOCOL)
        self.assertEqual(request['contexts'], [])
        self.assertNotIn('event', request)
        self.assertNotIn('persona', request)
        self.assertNotIn('model_report_excerpt', result)
        self.assertFalse(result['task_goal_verified'])

    async def test_qq_shell_and_publish_are_rejected(self):
        for name in ['send_message_to_user', 'astrbot_execute_shell', 'phone_delete', 'publish', 'future_task']:
            self.backend.calls.clear()
            result = await self.run_task(Planner([response([name])]))
            self.assertEqual(result['status'], 'rejected')
            self.assertEqual(self.backend.calls, [])

    async def test_arguments_cannot_smuggle_content_or_target(self):
        for args in [{'text': 'invented'}, {'command': 'adb shell'}, {'target': 'qq'}, {'scope': 'host'}]:
            result = await self.run_task(Planner([response(['phone_status'], [args])]))
            self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.backend.calls, [])

    async def test_action_arguments_are_validated_before_backend(self):
        for args in [
            {'command': 'adb shell input text hi'},
            {'target': '../../etc/passwd'},
            {'target': 'et_editor', 'extra': 'x'},
            {},
        ]:
            result = await self.run_task(Planner([response(['phone_tap'], [args])]), actions=('phone_tap',))
            self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.backend.calls, [])

    async def test_action_target_is_passed_to_backend(self):
        result = await self.run_task(
            Planner([response(['phone_tap'], [{'target': 'et_editor'}])]), actions=('phone_tap',),
        )
        self.assertEqual(result['status'], 'observations_returned')
        self.assertEqual(self.backend.calls, [('phone_tap', {'target': 'et_editor'})])

    async def test_input_requires_host_frozen_text(self):
        result = await self.run_task(
            Planner([response(['phone_input'], [{'target': 'et_editor'}])]), actions=('phone_input',),
        )
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.backend.calls, [])
        planner = Planner([response(['phone_input'], [{'target': 'et_editor'}])])
        result = await self.run_task(planner, actions=('phone_input',), frozen={'text': '原文'})
        self.assertEqual(result['status'], 'observations_returned')
        self.assertEqual(planner.requests[0]['prompt'].count('frozen by host'), 1)

    async def test_actions_can_repeat_and_result_stays_compact(self):
        planner = Planner([
            response(['phone_observe']),
            response(['phone_tap'], [{'target': 'et_editor'}]),
            response(['phone_observe']),
            response([]),
        ])
        result = await self.run_task(planner, actions=('phone_observe', 'phone_tap'), max_steps=6)
        self.assertEqual(result['status'], 'observations_returned')
        self.assertEqual([call[0] for call in self.backend.calls], ['phone_observe', 'phone_tap', 'phone_observe'])
        self.assertNotIn('observations', result)
        self.assertEqual(result['action_count'], 1)
        self.assertEqual(result['actions_taken'], [{'capability': 'phone_tap', 'result': {'state': 'device'}}])
        self.assertEqual(result['final']['capability'], 'phone_observe')
        self.assertEqual(result['external_action_status'], 'attempted')

    async def test_step_limit_keeps_partial_observations(self):
        planner = Planner([response(['phone_observe'])] * 3)
        result = await self.run_task(planner, actions=('phone_observe',), max_steps=2)
        self.assertEqual(result['status'], 'observations_returned')
        self.assertEqual(result['reason'], 'step_limit')
        self.assertEqual(len(self.backend.calls), 2)

    async def test_result_never_contains_control_tree(self):
        planner = Planner([response(['phone_observe'])])
        result = await self.run_task(planner, actions=('phone_observe',))
        self.assertNotIn('nodes', result['final'])
        self.assertEqual(result['final']['node_count'], 2)
        self.assertNotIn('目标文字', json.dumps(result, ensure_ascii=False))

    async def test_expect_text_returns_boolean_only(self):
        planner = Planner([response(['phone_observe'])])
        result = await self.run_task(planner, actions=('phone_observe',), frozen={'expect_text': '目标'})
        self.assertIs(result['expect_text_found'], True)
        planner = Planner([response(['phone_observe'])])
        result = await self.run_task(planner, actions=('phone_observe',), frozen={'expect_text': '不存在'})
        self.assertIs(result['expect_text_found'], False)

    async def test_batch_validated_before_any_action(self):
        result = await self.run_task(Planner([response(['phone_status', 'send_message_to_user'])]))
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.backend.calls, [])

    async def test_model_claim_is_discarded(self):
        result = await self.run_task(Planner([response(text='我替白回复QQ了')]))
        self.assertEqual(result['status'], 'needs_parent')
        self.assertNotIn('替白', json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.backend.calls, [])

    async def test_failure_not_retried(self):
        async def fail(name, args=None):
            self.backend.calls.append((name, args))
            raise TimeoutError()
        self.backend.execute = fail
        result = await self.run_task(Planner([response(['phone_status'])]))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.backend.calls), 1)

    async def test_cancel_propagates_and_records(self):
        async def cancel(name, args=None):
            raise asyncio.CancelledError()
        self.backend.execute = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.run_task(Planner([response(['phone_status'])]))
        result = json.loads(next(Path(self.directory.name).glob('*/result.json')).read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'cancelled')

    async def test_limits_and_goal_validated(self):
        for kwargs in [{'max_steps': 0}, {'max_steps': True}, {'tool_call_timeout': 61}]:
            with self.assertRaises(ValueError):
                await self.run_task(Planner([]), **kwargs)
