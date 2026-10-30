import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ..app.subagent import SandboxAgent


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
        self.requests.append({**kwargs, 'contexts': list(kwargs['contexts'])})
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
        payload = json.loads(planner.requests[0]['contexts'][0].content)
        self.assertRegex(payload['frozen_text_file'], r'^/tmp/operation-context/[a-f0-9]{32}/text\.txt$')
        self.assertIn(payload['frozen_text_file'], shell.commands[0])

    async def test_frozen_paths_are_isolated_per_task(self):
        paths = []
        for _ in range(2):
            planner = Planner([done('好')])
            shell = Shell()
            result = await self.run_agent(
                planner, shell, frozen_text='原文', frozen_files={'title': '标题', 'body': '正文'},
            )
            payload = json.loads(planner.requests[0]['contexts'][0].content)
            directory = f"/tmp/operation-context/{result['task_id']}"
            self.assertEqual(payload['frozen_text_file'], directory + '/text.txt')
            self.assertEqual(payload['frozen_files'], {
                'title': directory + '/title.txt', 'body': directory + '/body.txt',
            })
            for path in (payload['frozen_text_file'], *payload['frozen_files'].values()):
                self.assertIn(path, shell.commands[0])
            paths.append(payload['frozen_text_file'])
        self.assertNotEqual(*paths)

    async def test_failed_frozen_setup_never_starts_the_model(self):
        class FailedSetup:
            async def call(self, *args, **kwargs):
                return json.dumps({'exit_code': 1, 'stdout': '', 'stderr': 'write failed'})

        planner = Planner([done('不能执行')])
        result = await self.run_agent(planner, FailedSetup(), frozen_text='原文')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('frozen_payload_write_failed', result['summary'])
        self.assertEqual(planner.requests, [])

    async def test_step_limit_keeps_an_explicit_unfinished_result(self):
        planner = Planner([call('echo a') for _ in range(5)])
        shell = Shell()
        result = await self.run_agent(planner, shell, max_steps=2)
        self.assertEqual(result['status'], 'step_limit')
        self.assertEqual(result['command_count'], 2)
        self.assertEqual(result['reason'], 'step_limit')
        self.assertIn('2 commands ran', result['summary'])
        self.assertIn('No final conclusion', result['summary'])
        self.assertNotIn('echo a', result['summary'])
        self.assertEqual(len(planner.requests), 2)

    async def test_empty_final_response_is_not_completed(self):
        planner = Planner([done('   ')])
        result = await self.run_agent(planner, Shell())
        self.assertEqual(result['status'], 'needs_parent')
        self.assertEqual(result['reason'], 'empty_final_response')
        self.assertTrue(result['summary'])
        self.assertEqual(len(planner.requests), 1)

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
        result = await self.run_agent(planner, Verbose(), max_steps=20, needs_device_action=False)
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
        self.assertNotIn('echo same', result['summary'])

    async def test_full_parent_goal_with_preparation_rules_fits(self):
        from ..app.publish import build_goal

        planner = Planner([done('准备未提交')])
        result = await SandboxAgent(Shell(), None, self.root).run(
            context=planner, provider_id='p', goal=build_goal('目' * 4000, None, None),
        )
        self.assertEqual(result['status'], 'completed')
        self.assertIn('目' * 4000, planner.requests[0]['contexts'][0].content)

    async def test_idle_rounds_before_the_first_action_are_tolerated(self):
        planner = Planner([call('echo a'), call('echo b'), call('echo c'), done('看完了。')])
        result = await self.run_agent(planner, Shell())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['device_action_count'], 0)

    async def test_worker_protocol_disables_the_device_idle_rule(self):
        from ..app.subagent import SANDBOX_WORKER_PROTOCOL

        planner = Planner([call(f'echo {index}') for index in range(10)] + [done('写好了。')])
        result = await self.run_agent(
            planner, Shell(), protocol=SANDBOX_WORKER_PROTOCOL, needs_device_action=False,
        )
        self.assertEqual(result['status'], 'completed')
        self.assertIn('Do not touch the phone', planner.requests[0]['system_prompt'])

    async def test_read_only_exploration_with_new_output_is_not_stopped(self):
        class Changing:
            def __init__(self):
                self.count = 0

            async def call(self, run_context, command, background=False, timeout=None):
                self.count += 1
                return json.dumps({'exit_code': 0, 'stdout': f'page {self.count}', 'stderr': ''})

        planner = Planner(
            [call(f'cat page{index}') for index in range(12)] + [done('看完了。')],
        )
        result = await self.run_agent(planner, Changing())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['command_count'], 12)
        self.assertEqual(result['device_action_count'], 0)

    async def test_same_command_with_new_output_is_not_a_repeat(self):
        class Changing:
            def __init__(self):
                self.count = 0

            async def call(self, run_context, command, background=False, timeout=None):
                self.count += 1
                return json.dumps({'exit_code': 0, 'stdout': f'frame {self.count}', 'stderr': ''})

        planner = Planner(
            [call('adb shell uiautomator dump') for _ in range(5)] + [done('好。')],
        )
        result = await self.run_agent(planner, Changing())
        self.assertEqual(result['status'], 'completed')

    async def test_six_identical_rounds_after_acting_give_up(self):
        tap = '/workspace/.local/adbdl/platform-tools/adb -s 172.22.0.2:5555 shell input tap 1 1'
        planner = Planner([call(tap)] + [call(f'cat page{index}') for index in range(9)])
        result = await self.run_agent(planner, Shell())
        self.assertEqual(result['status'], 'needs_parent')
        self.assertEqual(result['reason'], 'no_progress')
        self.assertEqual(result['device_action_count'], 1)
        self.assertEqual(result['command_count'], 7)

    async def test_unchanged_observations_stop_even_when_actions_run(self):
        commands = [
            f'/workspace/.local/adbdl/platform-tools/adb -s 172.22.0.2:5555 shell input swipe '
            f'360 {900 + index} 360 400 300; cat /tmp/page'
            for index in range(9)
        ]
        shell = Shell({command: {'exit_code': 0, 'stdout': 'same page', 'stderr': ''}
                       for command in commands})
        result = await self.run_agent(Planner([call(command) for command in commands]), shell)
        self.assertEqual(result['status'], 'needs_parent')
        self.assertEqual(result['reason'], 'no_progress')
        self.assertEqual(result['command_count'], 7)
        self.assertEqual(result['device_action_count'], 7)

    async def test_empty_action_receipts_do_not_reset_unchanged_observations(self):
        responses, outputs = [], {}
        for index in range(9):
            action = (
                '/workspace/.local/adbdl/platform-tools/adb -s 172.22.0.2:5555 '
                f'shell input tap {index} 1'
            )
            observation = f'cat /tmp/page{index}'
            responses.extend([call(action), call(observation)])
            outputs[action] = {'exit_code': 0, 'stdout': '', 'stderr': ''}
            outputs[observation] = {'exit_code': 0, 'stdout': 'same page', 'stderr': ''}
        result = await self.run_agent(Planner(responses), Shell(outputs))
        self.assertEqual(result['status'], 'needs_parent')
        self.assertEqual(result['reason'], 'no_progress')
        self.assertEqual(result['command_count'], 14)

    async def test_changed_observations_reset_stagnation_after_actions(self):
        responses, outputs = [], {}
        for index in range(12):
            command = (
                '/workspace/.local/adbdl/platform-tools/adb -s 172.22.0.2:5555 '
                f'shell input tap {index} 1; cat /tmp/page'
            )
            responses.append(call(command))
            outputs[command] = {'exit_code': 0, 'stdout': f'page {index // 4}', 'stderr': ''}
        result = await self.run_agent(Planner(responses + [done('看完了。')]), Shell(outputs))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['command_count'], 12)

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

        from ..app import subagent

        planner = Planner([call('echo a') for _ in range(5)])
        original = subagent._stop_requested_at
        subagent._stop_requested_at = time.monotonic() + 10_000
        self.addCleanup(setattr, subagent, '_stop_requested_at', original)
        result = await self.run_agent(planner, Shell())
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['reason'], 'parent_requested_stop')
        self.assertIn('request of the host', result['summary'])

    async def test_time_budget_stops_before_the_next_round(self):
        planner = Planner([call('echo a')])
        result = await self.run_agent(planner, Shell(), time_budget=0)
        self.assertEqual(result['status'], 'time_budget')
        self.assertEqual(result['reason'], 'time_budget')
        self.assertIn('No final conclusion', result['summary'])
        self.assertEqual(planner.requests, [])

    async def test_rejected_tool_keeps_an_explicit_result(self):
        result = await self.run_agent(Planner([Response(['unknown_tool'], [{}])]), Shell())
        self.assertEqual(result['status'], 'rejected')
        self.assertTrue(result['reason'])
        self.assertTrue(result['summary'])

    async def test_work_context_is_saved_between_runs(self):
        from ..app.work_context import WorkContext

        work = WorkContext(self.root / 'work_contexts', 'task-1')
        planner = Planner([call('echo a'), done('好了。')])
        await self.run_agent(planner, Shell(), work_context=work)
        saved = work.load()
        roles = [item['role'] for item in saved]
        self.assertEqual(roles[0], 'user')
        self.assertIn('tool', roles)
        self.assertEqual(saved[-1]['role'], 'assistant')
        self.assertEqual(saved[-1]['content'], '好了。')

        resumed = Planner([call('echo next'), done('接着做完了。')])
        shell = Shell()
        result = await self.run_agent(resumed, shell, resume=saved, work_context=work)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(shell.commands, ['echo next'])
        self.assertEqual(len(resumed.requests), 2)
        seen = resumed.requests[0]['contexts']
        self.assertEqual(len(seen), len(saved) + 1)
        self.assertEqual(seen[0].role, 'user')
        self.assertTrue(all(hasattr(message, 'role') for message in seen))
        self.assertEqual(work.load()[-1]['content'], '接着做完了。')

    async def test_resumed_history_can_compact_old_tool_results(self):
        from ..app.work_context import WorkContext

        work = WorkContext(self.root / 'work_contexts', 'task-long')
        commands = [f'echo {index}' for index in range(10)]
        shell = Shell({command: {'exit_code': 0, 'stdout': 'x' * 800, 'stderr': ''}
                       for command in commands})
        first = await self.run_agent(
            Planner([call(command) for command in commands] + [done('上一轮结束。')]),
            shell, work_context=work, needs_device_action=False,
        )
        self.assertEqual(first['status'], 'completed')
        resumed = Planner([call('echo next'), done('接续结束。')])
        result = await self.run_agent(
            resumed, Shell(), resume=work.load(), work_context=work,
        )
        self.assertEqual(result['status'], 'completed')
        tools = [message for message in resumed.requests[1]['contexts'] if message.role == 'tool']
        self.assertEqual(len(tools), 11)
        self.assertIn('older result compacted', tools[0].content)

    async def test_followups_are_inserted_before_the_next_round(self):
        pending = ['顺便看一下电量']

        def take_followups():
            return pending.pop(0) if pending else []

        planner = Planner([call('echo a'), done('好。')])
        await self.run_agent(planner, Shell(), take_followups=take_followups)
        contents = [
            message.content for message in planner.requests[1]['contexts']
            if getattr(message, 'role', None) == 'user'
        ]
        self.assertTrue(any('运行中补充要求' in str(content) for content in contents))

    async def test_limits_are_validated(self):
        for kwargs in ({'max_steps': 0}, {'max_steps': 41}, {'max_steps': True}, {'goal': ''}):
            with self.assertRaises(ValueError):
                await SandboxAgent(Shell(), None, self.root).run(
                    context=Planner([]), provider_id='p', goal=kwargs.pop('goal', 'g'), **kwargs,
                )
