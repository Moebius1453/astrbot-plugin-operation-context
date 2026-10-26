import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.tool import FunctionTool, ToolSet

from .adapter import TOOL_NAME, resolve_persona, select_tools
from .redaction import sanitize_text


def tool(name, **kwargs):
    return FunctionTool(name=name, description='test', parameters={'type': 'object'}, **kwargs)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_intersection(self):
        candidates = ToolSet([tool('allowed'), tool('not-in-request'), tool('not-in-persona')])
        selected = select_tools(
            candidates, ['allowed', 'not-in-request', 'not-in-persona'],
            ['allowed', 'not-in-persona'], ['allowed', 'not-in-request'],
        )
        self.assertEqual([item.name for item in selected.tools], ['allowed'])

    async def test_recursive_and_background_tools_are_excluded(self):
        candidates = ToolSet([tool(TOOL_NAME), tool('background', is_background_task=True), tool('ok')])
        names = [item.name for item in candidates.tools]
        selected = select_tools(candidates, names, names, None)
        self.assertEqual([item.name for item in selected.tools], ['ok'])
        self.assertTrue(issubclass(HandoffTool, FunctionTool))

    async def test_missing_conversation_rejects_persona_fallback(self):
        context = SimpleNamespace(conversation_manager=SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value=None),
        ))
        with self.assertRaises(ValueError):
            await resolve_persona(context, SimpleNamespace(unified_msg_origin='test'))

    async def test_missing_authorized_snapshot_is_rejected(self):
        with self.assertRaises(TypeError):
            select_tools(ToolSet(), [], None, None)

    async def test_common_credentials_are_redacted(self):
        for original, secret in [
            ('Authorization: Bearer abcdef123', 'abcdef123'),
            ('api_key=very-secret-value', 'very-secret-value'),
            ('sk-abcdefghijklmnop', 'abcdefghijklmnop'),
        ]:
            self.assertNotIn(secret, sanitize_text(original))
