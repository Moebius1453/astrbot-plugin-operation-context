import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ..app.sandbox_agent import SandboxAgent


class Response:
    def __init__(self, names=None, arguments=None, text=''):
        self.tools_call_name = names or []
        self.tools_call_args = arguments or []
        self.tools_call_ids = [f'call_{index}' for index in range(len(self.tools_call_name))]
        self.completion_text = text

    def to_openai_tool_calls_model(self):
        return [
            {'id': call_id, 'type': 'function',
             'function': {'name': name, 'arguments': json.dumps(argument)}}
            for name, argument, call_id in zip(self.tools_call_name, self.tools_call_args, self.tools_call_ids)
        ]


class Planner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0) if self.responses else Response(text='')


def call(command):
    return Response(['sandbox_shell'], [{'command': command}])


def done(text):
    return Response(text=text)


class Shell:
    def __init__(self, results=None):
        self.commands = []
        self.results = results or {}

    async def call(self, run_context, command, background=False, timeout=None):
        self.commands.append(command)
        return json.dumps(self.results.get(command, {'exit_code': 0, 'stdout': 'ok', 'stderr': ''}))


class SandboxAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    async def run_agent(self, planner, shell, **kwargs):
        return await SandboxAgent(shell, None, self.root).run(
            context=planner, provider_id='p', goal='看看手机现在在哪个页面', **kwargs,
        )

    async def test_runs_commands_and_returns_summary_only(self):
        planner = Planner([
            call('/workspace/.local/adbdl/platform-tools/adb -s 172.22.0.2:5555 shell dumpsys window'),
            done('手机停在编辑页。'),
        ])
        shell = Shell()
        result = await self.run_agent(planner, shell)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['command_count'], 1)
        self.assertEqual(result['summary'], '手机停在编辑页。')
        self.assertEqual(result['external_action_status'], 'unknown')
        self.assertNotIn('stdout', json.dumps(result))
        record = Path(result['raw_record'])
        self.assertTrue((record / 'commands.jsonl').exists())
        self.assertTrue((record / 'result.json').exists())

    async def test_history_is_carried_between_turns(self):
        planner = Planner([call('echo a'), done('好了。')])
        await self.run_agent(planner, Shell())
        roles = [message.role for message in planner.requests[1]['contexts']]
        self.assertEqual(roles, ['user', 'assistant', 'tool'])
        self.assertEqual(planner.requests[1]['contexts'][2].tool_call_id, 'call_0')
        self.assertIn('ok', planner.requests[1]['contexts'][2].content)

    async def test_no_image_channel_is_used(self):
        planner = Planner([call('echo a'), done('好了。')])
        await self.run_agent(planner, Shell())
        for request in planner.requests:
            self.assertNotIn('image_urls', request)
        tools = planner.requests[0]['tools'].tools
        self.assertEqual([tool.name for tool in tools], ['sandbox_shell'])

    async def test_multiple_commands_in_one_turn_are_allowed(self):
        planner = Planner([
            Response(['sandbox_shell', 'sandbox_shell'], [{'command': 'echo a'}, {'command': 'echo b'}]),
            done('两条都跑了。'),
        ])
        shell = Shell()
        result = await self.run_agent(planner, shell)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['command_count'], 2)
        self.assertEqual(shell.commands, ['echo a', 'echo b'])

    async def test_rejects_tools_other_than_shell(self):
        planner = Planner([Response(['send_message_to_user'], [{}])])
        shell = Shell()
        result = await self.run_agent(planner, shell)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(shell.commands, [])

    async def test_rejects_overlong_or_empty_command(self):
        planner = Planner([call('x' * 2001)])
        shell = Shell()
        result = await self.run_agent(planner, shell)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(shell.commands, [])

    async def test_failure_reports_exception_text(self):
        class Broken:
            async def llm_generate(self, **kwargs):
                raise RuntimeError('quota exceeded for this key')

        result = await self.run_agent(Broken(), Shell())
        self.assertEqual(result['status'], 'failed')
        self.assertIn('quota exceeded', result['summary'])

    async def test_frozen_text_is_written_once_before_commands(self):
        planner = Planner([done('好')])
        shell = Shell()
        await self.run_agent(planner, shell, frozen_text='测试评论 @主人')
        self.assertEqual(len(shell.commands), 1)
        expected = base64.b64encode('测试评论 @主人'.encode()).decode()
        self.assertIn(expected, shell.commands[0])
        self.assertIn('/tmp/operation-frozen.txt', shell.commands[0])

    async def test_step_limit_stops_without_summary(self):
        planner = Planner([call('echo a') for _ in range(5)])
        shell = Shell()
        result = await self.run_agent(planner, shell, max_steps=2)
        self.assertEqual(result['status'], 'step_limit')
        self.assertEqual(result['command_count'], 2)
        self.assertIsNone(result['summary'])

    async def test_blocked_commands_never_reach_the_shell(self):
        planner = Planner([call('pm clear com.max.xiaoheihe'), done('被拦下了。')])
        shell = Shell()
        result = await self.run_agent(planner, shell)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(shell.commands, [])
        self.assertEqual(result['command_count'], 0)
        blocked = json.loads(planner.requests[1]['contexts'][2].content)
        self.assertEqual(blocked['reason'], 'package_mutation')
        record = Path(result['raw_record'])
        lines = (record / 'commands.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertTrue(any('"blocked"' in line for line in lines))

    async def test_device_action_count_is_reported(self):
        planner = Planner([
            call('/workspace/.local/adbdl/platform-tools/adb -s 172.22.0.2:5555 shell input tap 1 1'),
            done('点了一下。'),
        ])
        result = await self.run_agent(planner, Shell())
        self.assertEqual(result['device_action_count'], 1)

    async def test_old_results_are_compacted(self):
        class Verbose:
            async def call(self, run_context, command, background=False, timeout=None):
                return json.dumps({'exit_code': 0, 'stdout': 'x' * 800, 'stderr': ''})

        commands = [f'/workspace/.local/adbdl/platform-tools/adb -s 172.22.0.2:5555 shell input tap {index} 1'
                    for index in range(12)]
        planner = Planner([call(command) for command in commands] + [done('完')])
        result = await self.run_agent(planner, Verbose(), max_steps=20)
        self.assertEqual(result['status'], 'completed')
        texts = [message.content for message in planner.requests[-1]['contexts'] if message.role == 'tool']
        self.assertEqual(len(texts), 12)
        self.assertIn('older result compacted', texts[0])
        self.assertNotIn('older result compacted', texts[-1])

    async def test_repeating_one_command_three_times_gives_up(self):
        planner = Planner([call('echo same') for _ in range(5)])
        result = await self.run_agent(planner, Shell())
        self.assertEqual(result['status'], 'needs_parent')
        self.assertEqual(result['reason'], 'repeated_command')
        self.assertIn('three times', result['summary'])
        self.assertEqual(result['command_count'], 3)

    async def test_three_rounds_without_touching_the_phone_give_up(self):
        planner = Planner([call('echo a'), call('echo b'), call('echo c'), call('echo d')])
        result = await self.run_agent(planner, Shell())
        self.assertEqual(result['status'], 'needs_parent')
        self.assertEqual(result['reason'], 'no_progress')
        self.assertEqual(result['device_action_count'], 0)

    async def test_three_failed_commands_give_up(self):
        class Failing:
            async def call(self, run_context, command, background=False, timeout=None):
                return json.dumps({'exit_code': 1, 'stdout': '', 'stderr': 'boom'})

        planner = Planner([call('echo a'), call('echo b'), call('echo c'), call('echo d')])
        result = await self.run_agent(planner, Failing())
        self.assertEqual(result['status'], 'needs_parent')
        self.assertEqual(result['reason'], 'repeated_command_failure')

    async def test_host_stop_request_ends_the_round_cleanly(self):
        import time

        from ..app import sandbox_agent

        planner = Planner([call('echo a') for _ in range(5)])
        original = sandbox_agent._stop_requested_at
        sandbox_agent._stop_requested_at = time.monotonic() + 10_000
        self.addCleanup(setattr, sandbox_agent, '_stop_requested_at', original)
        result = await self.run_agent(planner, Shell())
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['reason'], 'parent_requested_stop')
        self.assertIn('request of the host', result['summary'])

    async def test_time_budget_stops_before_the_next_round(self):
        planner = Planner([call('echo a')])
        result = await self.run_agent(planner, Shell(), time_budget=0)
        self.assertEqual(result['status'], 'time_budget')
        self.assertEqual(planner.requests, [])

    async def test_limits_are_validated(self):
        for kwargs in ({'max_steps': 0}, {'max_steps': 41}, {'max_steps': True}, {'goal': ''}):
            with self.assertRaises(ValueError):
                await SandboxAgent(Shell(), None, self.root).run(
                    context=Planner([]), provider_id='p', goal=kwargs.pop('goal', 'g'), **kwargs,
                )
