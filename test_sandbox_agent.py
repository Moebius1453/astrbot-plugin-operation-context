import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from .sandbox_agent import SandboxAgent


class Planner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def llm_generate(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0) if self.responses else SimpleNamespace(
            tools_call_name=[], tools_call_args=[], completion_text='')


def call(command):
    return SimpleNamespace(tools_call_name=['sandbox_shell'], tools_call_args=[{'command': command}], completion_text='')


def done(text):
    return SimpleNamespace(tools_call_name=[], tools_call_args=[], completion_text=text)


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

    async def test_multiple_commands_in_one_turn_are_allowed(self):
        planner = Planner([
            SimpleNamespace(tools_call_name=['sandbox_shell', 'sandbox_shell'],
                            tools_call_args=[{'command': 'echo a'}, {'command': 'echo b'}],
                            completion_text=''),
            done('两条都跑了。'),
        ])
        shell = Shell()
        result = await self.run_agent(planner, shell)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['command_count'], 2)
        self.assertEqual(shell.commands, ['echo a', 'echo b'])

    async def test_rejects_tools_other_than_shell(self):
        planner = Planner([SimpleNamespace(
            tools_call_name=['send_message_to_user'], tools_call_args=[{}], completion_text='')])
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

    async def test_view_image_attaches_png_to_next_turn(self):
        planner = Planner([
            SimpleNamespace(tools_call_name=['view_image'],
                            tools_call_args=[{'path': '/workspace/phone-shot-abc.png'}],
                            completion_text=''),
            done('看到了编辑页。'),
        ])
        shell = Shell(results={"base64 -w0 '/workspace/phone-shot-abc.png'":
                               {'exit_code': 0, 'stdout': 'iVBORw0KGgo=', 'stderr': ''}})
        result = await self.run_agent(planner, shell)
        self.assertEqual(result['status'], 'completed')
        self.assertIsNone(planner.requests[0]['image_urls'])
        self.assertEqual(planner.requests[1]['image_urls'], ['data:image/png;base64,iVBORw0KGgo='])

    async def test_view_image_rejects_paths_outside_workspace(self):
        planner = Planner([SimpleNamespace(tools_call_name=['view_image'],
                                           tools_call_args=[{'path': '/etc/passwd'}], completion_text='')])
        shell = Shell()
        result = await self.run_agent(planner, shell)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(shell.commands, [])

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

    async def test_limits_are_validated(self):
        for kwargs in ({'max_steps': 0}, {'max_steps': True}, {'goal': ''}):
            with self.assertRaises(ValueError):
                await SandboxAgent(Shell(), None, self.root).run(
                    context=Planner([]), provider_id='p', goal=kwargs.pop('goal', 'g'), **kwargs,
                )
