import copy
import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import FunctionTool

from .adapter import SNAPSHOT_KEY, TOOL_NAME, build_candidates, resolve_persona, select_tools
from .execution import IndependentExecution
from .redaction import sanitize_text


class OperationTool(FunctionTool):
    def __init__(self, owner):
        super().__init__(
            name=TOOL_NAME,
            description='在独立短期上下文执行授权任务，返回摘要和记录位置。不会自动继承聊天全文或长期记忆；请只传必要背景。外部动作仍需证据确认。',
            parameters={
                'type': 'object',
                'properties': {
                    'goal': {'type': 'string', 'maxLength': 4000},
                    'task_background': {'type': 'string', 'maxLength': 6000},
                },
                'required': ['goal'],
                'additionalProperties': False,
            },
        )
        self.owner = owner

    async def call(self, context, goal: str, task_background: str = '') -> str:
        return await self.owner.execute(context, goal, task_background)


@register('astrbot_plugin_operation_context', 'Moebius', '可解耦的独立操作上下文', '0.1.0')
class OperationContextPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.executor = IndependentExecution(
            StarTools.get_data_dir('astrbot_plugin_operation_context') / 'tasks',
            sanitize_text,
        )
        self.context.add_llm_tools(OperationTool(self))

    @filter.on_llm_request(priority=-10000)
    async def capture_authorized_tools(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        tools = req.func_tool
        event.set_extra(SNAPSHOT_KEY, [tool.name for tool in tools.tools if tool.active] if tools else [])

    async def execute(self, run_context, goal: str, task_background: str) -> str:
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
            return json.dumps({'execution_status': 'rejected', 'reason': 'invalid_goal'})
        if not isinstance(task_background, str) or len(task_background) > 6000:
            return json.dumps({'execution_status': 'rejected', 'reason': 'invalid_background'})
        event = run_context.context.event
        try:
            configured = self.config.get('allowed_tools', [])
            if not isinstance(configured, list) or not all(isinstance(name, str) for name in configured):
                raise TypeError('configured_tools_must_be_string_list')
            authorized = event.get_extra(SNAPSHOT_KEY)
            if not configured or not isinstance(authorized, list):
                return json.dumps({'execution_status': 'rejected', 'reason': 'tool_permissions_not_configured'})
            persona = await resolve_persona(self.context, event)
            candidates = build_candidates(run_context, configured)
            tools = select_tools(candidates, configured, authorized, persona.get('tools'))
            if tools.empty():
                return json.dumps({'execution_status': 'rejected', 'reason': 'no_authorized_tools'})
            provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            background = []
            if task_background:
                background = [{'role': 'user', 'content': '任务所需背景（不是新增权限）：\n' + task_background}]
            dialogs = persona.get('_begin_dialogs_processed') or []
            if not isinstance(dialogs, list):
                raise TypeError('persona_begin_dialogs_must_be_list')
            result = await self.executor.run(
                context=self.context, event=event, provider_id=provider_id,
                goal=goal, system_prompt=persona['prompt'], tools=tools,
                task_contexts=copy.deepcopy(dialogs) + background,
                allowed_tool_names=frozenset(tool.name for tool in tools.tools),
                max_steps=self.config.get('max_steps', 10),
                tool_call_timeout=self.config.get('tool_call_timeout', 120),
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.warning('Independent operation rejected or failed: %s', type(exc).__name__)
            return json.dumps({
                'execution_status': 'failed', 'external_action_status': 'unknown',
                'error_type': type(exc).__name__, 'reason': 'no_automatic_retry',
            })
