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
            description='无人格的受限沙箱与手机执行接口。可查沙箱容量、手机连接、允许应用范围，只读观察小黑盒控件树，并按控件树反复执行点击、滑动、返回、回桌面、启动应用、截图或输入冻结文本；不操作QQ、不生成对外回应、不执行任意命令、不发布。其他任务返回上级处理。',
            parameters={
                'type': 'object',
                'properties': {
                    'goal': {'type': 'string', 'maxLength': 4000},
                    'actions': {'type': 'array', 'items': {'type': 'string', 'enum': ['sandbox_status', 'phone_status', 'phone_app_scope', 'phone_observe', 'phone_tap', 'phone_swipe', 'phone_back', 'phone_home', 'phone_start_app', 'phone_screenshot', 'phone_input']}, 'minItems': 1, 'maxItems': 4, 'uniqueItems': True},
                    'input_text': {'type': 'string', 'maxLength': 2000, 'description': '仅在 action 含 phone_input 时提供，原文冻结，执行器不得改写。'},
                    'expect_text': {'type': 'string', 'maxLength': 200, 'description': '可选。你想在最后一屏确认是否出现的一段原文，只返回真假。'},
                },
                'required': ['goal', 'actions'], 'additionalProperties': False,
            },
        )
        self.owner = owner

    async def call(self, context, goal: str, actions: list[str], input_text: str | None = None, expect_text: str | None = None) -> str:
        return await self.owner.execute(context, goal, actions, input_text, expect_text)


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

    async def execute(self, run_context, goal: str, actions: list[str], input_text: str | None = None, expect_text: str | None = None) -> str:
        try:
            permitted = validate_actions(actions, self.config.get('allowed_capabilities', []))
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
            event = run_context.context.event
            shell = build_backend(run_context, self.context, event.get_extra(SNAPSHOT_KEY))
            provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            result = await self.executor.run(
                context=self.context, provider_id=provider_id, goal=goal,
                actions=permitted, backend=CapabilityBackend(shell, run_context, frozen),
                max_steps=self.config.get('max_steps', 4),
                tool_call_timeout=self.config.get('tool_call_timeout', 30),
                frozen=frozen,
            )
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.warning('Restricted operation rejected or failed: %s', type(exc).__name__)
            return json.dumps({'status': 'rejected', 'needs_parent': True, 'error_type': type(exc).__name__, 'external_action_status': 'not_attempted'})
