import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import FunctionTool

from .adapter import SNAPSHOT_KEY, TOOL_NAME, CapabilityBackend, build_backend, validate_actions
from .execution import IndependentExecution
from .post_submit import PostSubmitter, validate_post
from .publish import DELETE_RULES, PREPARE_RULES, build_goal, validate_publish
from .redaction import sanitize_text
from .sandbox_agent import SandboxAgent


class OperationTool(FunctionTool):
    def __init__(self, owner):
        super().__init__(
            name=TOOL_NAME,
            description=(
                'Restricted, persona-free execution channel for the sandbox and the Android phone. '
                'Prefer it over running adb yourself: raw commands, control trees and screen dumps stay in the '
                'task record instead of the chat context. '
                'Give a goal and omit actions for open-ended phone work: the executor gets a shell and image '
                'viewing inside the sandbox, observes, switches methods and retries on its own, then returns one '
                'short summary plus an evidence path. '
                'For publishing use post_publish with post_title, post_text and optionally post_images and '
                'post_mention: the executor prepares the whole post, then the host verifies and submits it. '
                'post_delete removes one of your own posts by exact title. post_submit commits an editor you '
                'already filled by hand. '
                'Every outward word must be supplied by you through these fields, byte for byte; the executor '
                'never writes on your behalf, never touches QQ, and cannot publish comments. '
                'Handle anything outside this scope yourself.'
            ),
            parameters={
                'type': 'object',
                'properties': {
                    'goal': {'type': 'string', 'maxLength': 4000},
                    'actions': {'type': 'array', 'items': {'type': 'string', 'enum': ['sandbox_status', 'phone_status', 'phone_app_scope', 'phone_observe', 'phone_tap', 'phone_tap_at', 'phone_swipe', 'phone_back', 'phone_home', 'phone_start_app', 'phone_screenshot', 'phone_input', 'post_submit', 'post_publish', 'post_delete', 'sandbox_agent']}, 'minItems': 1, 'maxItems': 4, 'uniqueItems': True, 'description': 'Optional. Omit to let the executor work freely with a shell.'},
                    'input_text': {'type': 'string', 'maxLength': 2000, 'description': 'Exact text the executor may type or publish. It must not rewrite it.'},
                    'expect_text': {'type': 'string', 'maxLength': 200, 'description': 'Optional. Text you want confirmed on the final screen. Only a boolean is returned.'},
                    'post_title': {'type': 'string', 'maxLength': 100, 'description': 'Exact title for post_submit, post_publish or the post to remove for post_delete.'},
                    'post_text': {'type': 'string', 'maxLength': 4000, 'description': 'Exact post body for post_submit or post_publish.'},
                    'post_images': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 9, 'description': 'Optional image paths already inside the sandbox, for post_publish.'},
                    'post_mention': {'type': 'string', 'maxLength': 50, 'description': 'Optional nickname to mention with the native picker, for post_publish.'},
                },
                'required': ['goal'], 'additionalProperties': False,
            },
        )
        self.owner = owner

    async def call(self, context, goal: str, actions: list[str] | None = None, input_text: str | None = None, expect_text: str | None = None, post_title: str | None = None, post_text: str | None = None, post_images: list[str] | None = None, post_mention: str | None = None) -> str:
        return await self.owner.execute(context, goal, actions, input_text, expect_text, post_title, post_text, post_images, post_mention)


@register('astrbot_plugin_operation_context', 'Moebius', '无人格的受限沙箱与手机执行接口', '0.2.0')
class OperationContextPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.executor = IndependentExecution(
            StarTools.get_data_dir('astrbot_plugin_operation_context') / 'tasks', sanitize_text,
        )
        self.context.add_llm_tools(OperationTool(self))

    @filter.on_llm_request(priority=-10000)
    async def capture_authorized_tools(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        tools = req.func_tool
        event.set_extra(SNAPSHOT_KEY, [tool.name for tool in tools.tools if tool.active] if tools else [])

    async def execute(self, run_context, goal: str, actions: list[str] | None = None, input_text: str | None = None, expect_text: str | None = None, post_title: str | None = None, post_text: str | None = None, post_images: list[str] | None = None, post_mention: str | None = None) -> str:
        try:
            configured = self.config.get('allowed_capabilities', [])
            if actions is None or actions == []:
                permitted = validate_actions(['sandbox_agent'], configured)
            else:
                permitted = validate_actions(actions, configured)
            event = run_context.context.event
            shell = build_backend(run_context, self.context, event.get_extra(SNAPSHOT_KEY))

            async def run_agent(agent_goal, **kwargs):
                executor_provider = self.config.get('executor_provider')
                if isinstance(executor_provider, str) and executor_provider.strip():
                    provider = executor_provider.strip()
                else:
                    provider = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
                agent = SandboxAgent(
                    shell, run_context,
                    StarTools.get_data_dir('astrbot_plugin_operation_context') / 'sandbox_agent',
                )
                return await agent.run(
                    context=self.context, provider_id=provider, goal=agent_goal,
                    max_steps=self.config.get('max_steps', 10),
                    command_timeout=self.config.get('tool_call_timeout', 60), **kwargs,
                )

            if 'sandbox_agent' in permitted:
                if permitted != ('sandbox_agent',):
                    raise ValueError('sandbox_agent_must_be_alone')
                if expect_text is not None or post_title is not None or post_text is not None:
                    raise ValueError('unexpected_text_fields')
                if input_text is not None and (
                    not isinstance(input_text, str) or not input_text.strip() or len(input_text) > 2000
                ):
                    raise ValueError('invalid_input_text')
                return json.dumps(await run_agent(goal, frozen_text=input_text), ensure_ascii=False)
            if 'post_publish' in permitted:
                if permitted != ('post_publish',):
                    raise ValueError('post_publish_must_be_alone')
                if input_text is not None or expect_text is not None:
                    raise ValueError('unexpected_text_fields')
                validate_publish(post_title, post_text, post_images, post_mention)
                preparation = await run_agent(
                    build_goal(post_images, post_mention),
                    frozen_files={'title': post_title, 'body': post_text},
                    extra_rules=PREPARE_RULES,
                )
                result = {
                    'preparation': {
                        'status': preparation['status'], 'summary': preparation['summary'],
                        'command_count': preparation['command_count'],
                        'raw_record': preparation['raw_record'],
                    },
                    'submit': {'status': 'skipped', 'reason': 'preparation_not_completed'},
                }
                if preparation['status'] == 'completed':
                    async def execute_command(command):
                        return json.loads(await shell.call(
                            run_context, command=command, background=False, timeout=120,
                        ))
                    submitter = PostSubmitter(
                        execute_command,
                        StarTools.get_data_dir('astrbot_plugin_operation_context') / 'post_submissions',
                    )
                    result['submit'] = await submitter.submit(post_title, post_text)
                return json.dumps(result, ensure_ascii=False)
            if 'post_delete' in permitted:
                if permitted != ('post_delete',):
                    raise ValueError('post_delete_must_be_alone')
                if input_text is not None or expect_text is not None or post_text is not None:
                    raise ValueError('unexpected_text_fields')
                if not isinstance(post_title, str) or not post_title.strip() or len(post_title) > 100:
                    raise ValueError('invalid_post_title')
                return json.dumps(await run_agent(
                    'Delete one of your own posts. The exact title is in /tmp/operation-target.txt.',
                    frozen_files={'target': post_title},
                    extra_rules=DELETE_RULES,
                ), ensure_ascii=False)
            if 'post_submit' in permitted:
                if permitted != ('post_submit',):
                    raise ValueError('post_submit_must_be_alone')
                if input_text is not None or expect_text is not None:
                    raise ValueError('unexpected_text_fields')
                validate_post(post_title, post_text)

                async def execute_command(command):
                    return json.loads(await shell.call(
                        run_context, command=command, background=False, timeout=120,
                    ))

                submitter = PostSubmitter(
                    execute_command,
                    StarTools.get_data_dir('astrbot_plugin_operation_context') / 'post_submissions',
                )
                return json.dumps(await submitter.submit(post_title, post_text), ensure_ascii=False)
            if post_title is not None or post_text is not None:
                raise ValueError('unexpected_post_fields')
            frozen = {}
            if 'phone_input' in permitted:
                if not isinstance(input_text, str) or not input_text.strip() or len(input_text) > 2000:
                    raise ValueError('input_text_required')
                frozen['text'] = input_text
            elif input_text is not None:
                raise ValueError('unexpected_input_text')
            if expect_text is not None:
                if not isinstance(expect_text, str) or not expect_text.strip() or len(expect_text) > 200:
                    raise ValueError('invalid_expect_text')
                frozen['expect_text'] = expect_text
            executor_provider = self.config.get('executor_provider')
            if isinstance(executor_provider, str) and executor_provider.strip():
                provider_id = executor_provider.strip()
            else:
                provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            result = await self.executor.run(
                context=self.context, provider_id=provider_id, goal=goal,
                actions=permitted, backend=CapabilityBackend(shell, run_context, frozen),
                max_steps=self.config.get('max_steps', 4),
                tool_call_timeout=self.config.get('tool_call_timeout', 30),
                frozen=frozen, vision=True,
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.warning('Restricted operation rejected or failed: %s', type(exc).__name__)
            return json.dumps({'status': 'rejected', 'needs_parent': True, 'error_type': type(exc).__name__, 'external_action_status': 'not_attempted'})
