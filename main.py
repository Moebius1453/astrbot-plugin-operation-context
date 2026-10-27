import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import FunctionTool

from .adapter import SNAPSHOT_KEY, TOOL_NAME, CapabilityBackend, build_backend, resolve_actions
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
                'short summary plus an evidence path. Its commands are screened first; clearing app data, '
                'changing system settings, installing packages, rebooting and non-input broadcasts are refused. '
                'For publishing use post_publish with post_title and post_text: the executor prepares the '
                'whole post, then the host verifies and submits it. Attachments are not supported yet. '
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
                    'actions': {'type': 'array', 'items': {'type': 'string', 'enum': ['sandbox_status', 'phone_status', 'phone_app_scope', 'phone_observe', 'phone_tap', 'phone_swipe', 'phone_back', 'phone_home', 'phone_start_app', 'phone_input', 'post_submit', 'post_publish', 'post_delete', 'sandbox_agent']}, 'minItems': 1, 'maxItems': 4, 'uniqueItems': True, 'description': 'Optional. Omit to let the executor work freely with a shell.'},
                    'input_text': {'type': 'string', 'maxLength': 2000, 'description': 'Exact text the executor may type or publish. It must not rewrite it.'},
                    'expect_text': {'type': 'string', 'maxLength': 200, 'description': 'Optional. Text you want confirmed on the final screen. Only a boolean is returned.'},
                    'post_title': {'type': 'string', 'maxLength': 100, 'description': 'Exact title for post_submit, post_publish or the post to remove for post_delete.'},
                    'post_text': {'type': 'string', 'maxLength': 4000, 'description': 'Exact post body for post_submit or post_publish.'},
                    'post_images': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 0, 'description': 'Not supported yet. Leave empty.'},
                    'post_mention': {'type': 'string', 'maxLength': 50, 'description': 'Optional nickname to mention with the native picker, for post_publish.'},
                },
                'required': ['goal'], 'additionalProperties': False,
            },
        )
        self.owner = owner

    async def call(self, context, goal: str, actions: list[str] | None = None, input_text: str | None = None, expect_text: str | None = None, post_title: str | None = None, post_text: str | None = None, post_images: list[str] | None = None, post_mention: str | None = None) -> str:
        return await self.owner.execute(context, goal, actions, input_text, expect_text, post_title, post_text, post_images, post_mention)


class TaskRequest:
    __slots__ = (
        'run_context', 'shell', 'goal', 'permitted', 'input_text', 'expect_text',
        'post_title', 'post_text', 'post_images', 'post_mention', 'time_budget',
    )

    def __init__(self, *, run_context, shell, goal, permitted, input_text, expect_text,
                 post_title, post_text, post_images, post_mention, time_budget):
        self.run_context = run_context
        self.shell = shell
        self.goal = goal
        self.permitted = permitted
        self.input_text = input_text
        self.expect_text = expect_text
        self.post_title = post_title
        self.post_text = post_text
        self.post_images = post_images
        self.post_mention = post_mention
        self.time_budget = time_budget


@register('astrbot_plugin_operation_context', 'Moebius', '无人格的受限沙箱与手机执行接口', '0.2.0')
class OperationContextPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.executor = IndependentExecution(
            StarTools.get_data_dir('astrbot_plugin_operation_context') / 'tasks', sanitize_text,
        )
        self.outward_handlers = {
            'sandbox_agent': self.handle_agent,
            'post_publish': self.handle_publish,
            'post_delete': self.handle_delete,
            'post_submit': self.handle_submit,
        }
        self.context.add_llm_tools(OperationTool(self))

    @filter.on_llm_request(priority=-10000)
    async def capture_authorized_tools(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        tools = req.func_tool
        event.set_extra(SNAPSHOT_KEY, [tool.name for tool in tools.tools if tool.active] if tools else [])

    async def execute(self, run_context, goal: str, actions: list[str] | None = None, input_text: str | None = None, expect_text: str | None = None, post_title: str | None = None, post_text: str | None = None, post_images: list[str] | None = None, post_mention: str | None = None) -> str:
        try:
            permitted, outward = resolve_actions(actions, self.config.get('allowed_capabilities', []))
            event = run_context.context.event
            settings = self.context.get_config(umo=event.unified_msg_origin).get('provider_settings', {})
            limit = settings.get('tool_call_timeout')
            time_budget = max(30, float(limit) - 10) if isinstance(limit, (int, float)) else 110.0
            request = TaskRequest(
                run_context=run_context, shell=build_backend(run_context, self.context, event.get_extra(SNAPSHOT_KEY)),
                goal=goal, permitted=permitted, input_text=input_text, expect_text=expect_text,
                post_title=post_title, post_text=post_text, post_images=post_images, post_mention=post_mention,
                time_budget=time_budget,
            )
            if outward is not None:
                return await self.outward_handlers[outward](request)
            return await self.run_fixed(request)
        except Exception as exc:
            logger.warning('Restricted operation rejected or failed: %s', type(exc).__name__)
            return json.dumps({'status': 'rejected', 'needs_parent': True, 'error_type': type(exc).__name__, 'external_action_status': 'not_attempted'})

    async def provider_id(self, event) -> str:
        configured = self.config.get('executor_provider')
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return await self.context.get_current_chat_provider_id(event.unified_msg_origin)

    async def run_agent(self, request: TaskRequest, goal: str, **kwargs) -> dict:
        agent = SandboxAgent(
            request.shell, request.run_context,
            StarTools.get_data_dir('astrbot_plugin_operation_context') / 'sandbox_agent',
        )
        return await agent.run(
            context=self.context, provider_id=await self.provider_id(request.run_context.context.event),
            goal=goal, max_steps=self.config.get('max_steps', 30),
            command_timeout=self.config.get('tool_call_timeout', 60),
            time_budget=request.time_budget, **kwargs,
        )

    async def submitter(self, request: TaskRequest) -> PostSubmitter:
        async def execute_command(command):
            return json.loads(await request.shell.call(
                request.run_context, command=command, background=False, timeout=120,
            ))
        return PostSubmitter(
            execute_command,
            StarTools.get_data_dir('astrbot_plugin_operation_context') / 'post_submissions',
        )

    async def handle_agent(self, request: TaskRequest) -> str:
        if any(value is not None for value in (request.expect_text, request.post_title, request.post_text)):
            raise ValueError('unexpected_text_fields')
        if request.input_text is not None and (
            not isinstance(request.input_text, str) or not request.input_text.strip() or len(request.input_text) > 2000
        ):
            raise ValueError('invalid_input_text')
        result = await self.run_agent(request, request.goal, frozen_text=request.input_text)
        return json.dumps(result, ensure_ascii=False)

    async def handle_publish(self, request: TaskRequest) -> str:
        if request.input_text is not None or request.expect_text is not None:
            raise ValueError('unexpected_text_fields')
        validate_publish(request.post_title, request.post_text, request.post_images, request.post_mention)
        preparation = await self.run_agent(
            request, build_goal(request.post_images, request.post_mention),
            frozen_files={'title': request.post_title, 'body': request.post_text},
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
            submitter = await self.submitter(request)
            result['submit'] = await submitter.submit(request.post_title, request.post_text)
        return json.dumps(result, ensure_ascii=False)

    async def handle_delete(self, request: TaskRequest) -> str:
        if any(value is not None for value in (request.input_text, request.expect_text, request.post_text)):
            raise ValueError('unexpected_text_fields')
        if not isinstance(request.post_title, str) or not request.post_title.strip() or len(request.post_title) > 100:
            raise ValueError('invalid_post_title')
        result = await self.run_agent(
            request, 'Delete one of your own posts. The exact title is in /tmp/operation-target.txt.',
            frozen_files={'target': request.post_title}, extra_rules=DELETE_RULES,
        )
        return json.dumps(result, ensure_ascii=False)

    async def handle_submit(self, request: TaskRequest) -> str:
        if request.input_text is not None or request.expect_text is not None:
            raise ValueError('unexpected_text_fields')
        validate_post(request.post_title, request.post_text)
        submitter = await self.submitter(request)
        return json.dumps(await submitter.submit(request.post_title, request.post_text), ensure_ascii=False)

    async def run_fixed(self, request: TaskRequest) -> str:
        if request.post_title is not None or request.post_text is not None or request.post_mention not in (None, ''):
            raise ValueError('unexpected_post_fields')
        frozen = {}
        if 'phone_input' in request.permitted:
            if not isinstance(request.input_text, str) or not request.input_text.strip() or len(request.input_text) > 2000:
                raise ValueError('input_text_required')
            frozen['text'] = request.input_text
        elif request.input_text is not None:
            raise ValueError('unexpected_input_text')
        if request.expect_text is not None:
            if not isinstance(request.expect_text, str) or not request.expect_text.strip() or len(request.expect_text) > 200:
                raise ValueError('invalid_expect_text')
            frozen['expect_text'] = request.expect_text
        result = await self.executor.run(
            context=self.context, provider_id=await self.provider_id(request.run_context.context.event),
            goal=request.goal, actions=request.permitted,
            backend=CapabilityBackend(request.shell, request.run_context, frozen),
            max_steps=self.config.get('max_steps', 4),
            tool_call_timeout=self.config.get('tool_call_timeout', 30),
            frozen=frozen, time_budget=request.time_budget,
        )
        return json.dumps(result, ensure_ascii=False)
