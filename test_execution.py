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
        return next(self.responses)


def response(names=(), args=None, text=''):
    return SimpleNamespace(tools_call_name=list(names), tools_call_args=args if args is not None else [{} for _ in names], completion_text=text)


class Backend:
    def __init__(self):
        self.calls = []

    async def execute(self, name):
        self.calls.append(name)
        return {'state': 'device'}


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.executor = IndependentExecution(Path(self.directory.name), lambda text: text)
        self.backend = Backend()

    async def run_task(self, planner, **kwargs):
        return await self.executor.run(context=planner, provider_id='fake', goal='status', actions=('phone_status',), backend=self.backend, **kwargs)

    async def test_observation_without_final_text(self):
        planner = Planner([response(['phone_status'])])
        result = await self.run_task(planner)
        self.assertEqual(result['status'], 'observations_returned')
        self.assertEqual(self.backend.calls, ['phone_status'])
        request = planner.requests[0]
        self.assertEqual(request['system_prompt'], EXECUTION_PROTOCOL)
        self.assertEqual(request['contexts'], [])
        self.assertNotIn('event', request)
        self.assertNotIn('persona', request)
        self.assertNotIn('model_report_excerpt', result)
        self.assertFalse(result['task_goal_verified'])

    async def test_qq_shell_and_publish_are_rejected(self):
        for name in ['send_message_to_user', 'astrbot_execute_shell', 'phone_tap', 'publish', 'future_task']:
            self.backend.calls.clear()
            result = await self.run_task(Planner([response([name])]))
            self.assertEqual(result['status'], 'rejected')
            self.assertEqual(self.backend.calls, [])

    async def test_arguments_cannot_smuggle_content_or_target(self):
        for args in [{'text': 'invented'}, {'command': 'adb shell'}, {'target': 'qq'}, {'scope': 'host'}]:
            result = await self.run_task(Planner([response(['phone_status'], [args])]))
            self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.backend.calls, [])

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
        async def fail(name):
            self.backend.calls.append(name)
            raise TimeoutError()
        self.backend.execute = fail
        result = await self.run_task(Planner([response(['phone_status'])]))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.backend.calls), 1)

    async def test_cancel_propagates_and_records(self):
        async def cancel(name):
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
