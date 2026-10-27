import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import FunctionTool

from .adapter import SNAPSHOT_KEY, TOOL_NAME, CapabilityBackend, build_backend, validate_actions
from .execution import IndependentExecution
from .redaction import sanitize_text


class OperationTool(FunctionTool):
    def __init__(self, owner):
        super().__init__(
            name=TOOL_NAME,
            description='无人格的受限状态查询接口。仅支持沙箱容量和手机连接状态；不操作QQ、不生成对外回应、不执行任意命令或发送。其他任务返回上级处理。',
            parameters={
                'type': 'object',
                'properties': {
                    'goal': {'type': 'string', 'maxLength': 4000},
                    'actions': {'type': 'array', 'items': {'type': 'string', 'enum': ['sandbox_status', 'phone_status']}, 'minItems': 1, 'maxItems': 2, 'uniqueItems': True},
                },
                'required': ['goal', 'actions'], 'additionalProperties': False,
            },
        )
        self.owner = owner

    async def call(self, context, goal: str, actions: list[str]) -> str:
        return await self.owner.execute(context, goal, actions)


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

    async def execute(self, run_context, goal: str, actions: list[str]) -> str:
        try:
            permitted = validate_actions(actions, self.config.get('allowed_capabilities', []))
            event = run_context.context.event
            shell = build_backend(run_context, self.context, event.get_extra(SNAPSHOT_KEY))
            provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            result = await self.executor.run(
                context=self.context, provider_id=provider_id, goal=goal,
                actions=permitted, backend=CapabilityBackend(shell, run_context),
                max_steps=self.config.get('max_steps', 4),
                tool_call_timeout=self.config.get('tool_call_timeout', 30),
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.warning('Restricted operation rejected or failed: %s', type(exc).__name__)
            return json.dumps({'status': 'rejected', 'needs_parent': True, 'error_type': type(exc).__name__, 'external_action_status': 'not_attempted'})
