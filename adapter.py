from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor


TOOL_NAME = 'operation_context_run'
SNAPSHOT_KEY = 'operation_context_authorized_tools'


def select_tools(candidates: ToolSet, configured: list[str], authorized: list[str], persona_tools) -> ToolSet:
    if not isinstance(configured, list) or not all(isinstance(name, str) for name in configured):
        raise TypeError('configured_tools_must_be_string_list')
    if not isinstance(authorized, list) or not all(isinstance(name, str) for name in authorized):
        raise TypeError('authorized_tools_snapshot_missing')
    if persona_tools is not None and (
        not isinstance(persona_tools, list) or not all(isinstance(name, str) for name in persona_tools)
    ):
        raise TypeError('invalid_persona_tools')
    allowed = set(configured) & set(authorized)
    if persona_tools is not None:
        allowed &= set(persona_tools)
    return ToolSet([
        tool for tool in candidates.tools
        if tool.name in allowed
        and tool.name != TOOL_NAME
        and tool.active
        and not tool.is_background_task
        and not isinstance(tool, HandoffTool)
    ])


def build_candidates(run_context, names: list[str]) -> ToolSet:
    # 私有运行时工具适配只集中在这里，接口变化时拒绝委派而非放宽权限。
    tools = FunctionToolExecutor._build_handoff_toolset(run_context, names)
    return tools if tools is not None else ToolSet()


async def resolve_persona(context, event):
    umo = event.unified_msg_origin
    conversation_id = await context.conversation_manager.get_curr_conversation_id(umo)
    if not conversation_id:
        raise ValueError('current_conversation_missing')
    conversation = await context.conversation_manager.get_conversation(umo, conversation_id)
    if conversation is None:
        raise ValueError('current_conversation_missing')
    _, persona, _, _ = await context.persona_manager.resolve_selected_persona(
        umo=umo,
        conversation_persona_id=conversation.persona_id,
        platform_name=event.get_platform_name(),
        provider_settings=context.get_config(umo=umo).get('provider_settings', {}),
    )
    if not persona or not isinstance(persona.get('prompt'), str) or not persona['prompt'].strip():
        raise ValueError('current_persona_missing')
    return persona
