import json
import re
import shlex

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

from .phone_actions import action_command, parse_receipt
from .phone_observation import describe, observe_phone
from .threat_detector import PromptThreatDetector


TAINT_SCORE = 5
_DETECTOR = PromptThreatDetector()

PRE_ACTION_ERRORS = (
    'missing_or_ambiguous_target', 'target_not_interactive', 'invalid_target_bounds',
    'unsafe_target_bounds', 'hierarchy_unavailable', 'foreign_package',
    'phone_outside_allowed_app', 'unsafe_coordinates', 'editor_required',
    'post_type_not_normal', 'editor_field_missing', 'editor_content_mismatch',
    'existing_draft_must_not_change', 'trusted_input_method_required',
)


def action_failure(envelope):
    detail = envelope.get('stderr')
    if not isinstance(detail, str):
        return 'action_failed_state_unknown'
    for marker in PRE_ACTION_ERRORS:
        if marker in detail:
            return 'pre_action:' + marker
    return 'action_failed_state_unknown'


def screen_observation(description: dict) -> dict:
    texts = [
        node['text'] for node in description['nodes']
        if isinstance(node.get('text'), str) and node['text']
    ]
    if not texts:
        return description
    report = _DETECTOR.analyze('\n'.join(texts))
    if report['score'] < TAINT_SCORE:
        return description
    for node in description['nodes']:
        if isinstance(node.get('text'), str) and node['text']:
            node['text'] = '[已屏蔽]'
    description['text_available'] = False
    description['tainted'] = report['severity']
    return description


TOOL_NAME = 'operation_context_run'
SNAPSHOT_KEY = 'operation_context_authorized_tools'
CAPABILITIES = frozenset({
    'sandbox_status', 'phone_status', 'phone_app_scope', 'phone_observe', 'phone_events',
    'phone_tap', 'phone_swipe', 'phone_back', 'phone_home', 'phone_start_app',
    'phone_input', 'post_submit', 'post_publish', 'post_delete', 'comment_publish', 'sandbox_agent',
})
ACTION_CAPABILITIES = frozenset({
    'phone_tap', 'phone_swipe', 'phone_back', 'phone_home', 'phone_start_app', 'phone_input',
})
# Tier 1 reads state, tier 2 changes the phone but stays reversible inside the app,
# tier 3 can be seen by someone outside this conversation. Tier 3 never shares a task.
CAPABILITY_TIERS = {
    'sandbox_status': 1, 'phone_status': 1, 'phone_app_scope': 1, 'phone_observe': 1,
    'phone_events': 1,
    'phone_tap': 2, 'phone_swipe': 2, 'phone_back': 2, 'phone_home': 2,
    'phone_start_app': 2, 'phone_input': 2,
    'post_submit': 3, 'post_publish': 3, 'post_delete': 3, 'comment_publish': 3,
    'sandbox_agent': 3,
}
# Capabilities that never share a task, whether or not they act outwardly.
STANDALONE_CAPABILITIES = frozenset({
    'phone_events', 'post_submit', 'post_publish', 'post_delete', 'comment_publish', 'sandbox_agent',
})
SHELL_NAME = 'astrbot_execute_shell'

_SANDBOX_SCRIPT = '''import json, os, shutil
s = shutil.disk_usage('/workspace')
print(json.dumps({'workspace_exists': os.path.isdir('/workspace'), 'free_bytes': s.free, 'total_bytes': s.total}))
'''
_PHONE_SCRIPT = '''import json, os, subprocess
adb = '/workspace/.local/adbdl/platform-tools/adb'
if not os.path.isfile(adb):
    adb = '/usr/bin/adb'
if not os.path.isfile(adb):
    print(json.dumps({'state': 'client_missing'}))
else:
    r = subprocess.run([adb, '-s', '172.22.0.2:5555', 'get-state'], capture_output=True, text=True, timeout=10)
    state = r.stdout.strip()
    print(json.dumps({'state': state if state in ('device', 'offline', 'unknown') else 'unavailable', 'exit_code': r.returncode}))
'''
_APP_SCOPE_SCRIPT = '''import json, os, re, subprocess
adb = '/workspace/.local/adbdl/platform-tools/adb'
if not os.path.isfile(adb):
    adb = '/usr/bin/adb'
if not os.path.isfile(adb):
    raise RuntimeError('adb_client_missing')
r = subprocess.run([adb, '-s', '172.22.0.2:5555', 'shell', 'dumpsys', 'activity', 'activities'], capture_output=True, text=True, timeout=10)
if r.returncode != 0:
    raise RuntimeError('foreground_query_failed')
packages = re.findall(r'(?:mResumedActivity:|topResumedActivity=|ResumedActivity:).*?\\s([A-Za-z0-9_.]+)/', r.stdout)
print(json.dumps({'scope': 'allowed_app' if packages and set(packages) == {'com.max.xiaoheihe'} else 'blocked_or_unknown'}))
'''
COMMANDS = {
    'phone_app_scope': 'python3 -I -c ' + shlex.quote(_APP_SCOPE_SCRIPT),
    'sandbox_status': 'python3 -I -c ' + shlex.quote(_SANDBOX_SCRIPT),
    'phone_status': 'python3 -I -c ' + shlex.quote(_PHONE_SCRIPT),
}


def validate_actions(actions, configured):
    if not isinstance(actions, list) or not actions or len(actions) > 4:
        raise ValueError('actions_must_be_nonempty_list_up_to_four')
    if not isinstance(configured, list) or not all(isinstance(x, str) for x in configured):
        raise ValueError('invalid_capability_configuration')
    if not all(isinstance(x, str) and x in CAPABILITIES and x in configured for x in actions):
        raise ValueError('capability_not_authorized')
    if len(set(actions)) != len(actions):
        raise ValueError('duplicate_action')
    return tuple(actions)


def resolve_actions(actions, configured):
    """Return the permitted capabilities and the one that must run alone.

    A capability that can be seen from outside this conversation, or that
    answers from a different source than the phone driver, never shares a task:
    the host could no longer say which part produced the result.
    """
    if actions is None or actions == []:
        actions = ['sandbox_agent']
    permitted = validate_actions(actions, configured)
    alone = [name for name in permitted if name in STANDALONE_CAPABILITIES]
    if alone and len(permitted) > 1:
        raise ValueError('standalone_capability_must_be_alone')
    return permitted, alone[0] if alone else None


ACTION_SCHEMAS = {
    'phone_tap': {
        'type': 'object',
        'properties': {
            'target': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'match': {'type': 'string', 'enum': ['id', 'text']},
        },
        'required': ['target'], 'additionalProperties': False,
    },
    'phone_input': {
        'type': 'object',
        'properties': {'target': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
        'required': ['target'], 'additionalProperties': False,
    },
    'phone_back': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    'phone_home': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    'phone_start_app': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    'phone_swipe': {
        'type': 'object',
        'properties': {
            'target': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'direction': {'type': 'string', 'enum': ['up', 'down', 'left', 'right']},
        },
        'required': ['target', 'direction'], 'additionalProperties': False,
    },
}


def validate_arguments(action, arguments, frozen):
    schema = ACTION_SCHEMAS.get(action)
    if schema is None:
        if arguments != {}:
            raise ValueError('arguments_not_allowed')
        return {}
    if type(arguments) is not dict:
        raise ValueError('invalid_arguments')
    if set(arguments) - set(schema['properties']):
        raise ValueError('unknown_argument')
    if set(schema.get('required', ())) - set(arguments):
        raise ValueError('missing_argument')
    for key, value in arguments.items():
        spec = schema['properties'][key]
        if spec.get('type') == 'integer':
            if type(value) is not int:
                raise ValueError('invalid_argument_value')
            if not spec.get('minimum', -2**31) <= value <= spec.get('maximum', 2**31):
                raise ValueError('invalid_argument_value')
            continue
        if not isinstance(value, str) or not value or len(value) > 200:
            raise ValueError('invalid_argument_value')
        if any(ord(character) < 32 for character in value):
            raise ValueError('invalid_argument_value')
    if arguments.get('match', 'id') not in ('id', 'text'):
        raise ValueError('invalid_match')
    if 'direction' in arguments and arguments['direction'] not in ('up', 'down', 'left', 'right'):
        raise ValueError('invalid_direction')
    if 'target' in arguments and arguments.get('match', 'id') == 'id' and not re.fullmatch(r'[A-Za-z0-9_.]+', arguments['target']):
        raise ValueError('invalid_resource_id')
    if action == 'phone_input':
        text = frozen.get('text') if isinstance(frozen, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('frozen_text_required')
    return arguments


def build_backend(run_context, context, authorized):
    umo = run_context.context.event.unified_msg_origin
    settings = context.get_config(umo=umo).get('provider_settings', {})
    if settings.get('computer_use_runtime') != 'sandbox':
        raise ValueError('sandbox_runtime_required')
    if settings.get('sandbox', {}).get('booter') != 'shipyard_neo':
        raise ValueError('shipyard_neo_required')
    if not isinstance(authorized, list) or SHELL_NAME not in authorized:
        raise ValueError('parent_shell_permission_required')
    candidates = FunctionToolExecutor._build_handoff_toolset(run_context, [SHELL_NAME])
    candidate = candidates.get_tool(SHELL_NAME) if candidates else None
    from astrbot.core.tools.computer_tools.shell import ExecuteShellTool
    if type(candidate) is not ExecuteShellTool or not candidate.active or candidate.is_background_task:
        raise ValueError('trusted_shell_backend_unavailable')
    return candidate


class CapabilityBackend:
    def __init__(self, shell, run_context, frozen=None):
        self.shell = shell
        self.run_context = run_context
        self.frozen = frozen or {}

    async def _envelope(self, command, timeout):
        raw = await self.shell.call(
            self.run_context, command=command, background=False, timeout=timeout,
        )
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            raise ValueError('invalid_shell_envelope')
        return envelope

    async def execute(self, action, arguments=None):
        if action not in CAPABILITIES:
            raise ValueError('unsupported_capability')
        arguments = validate_arguments(action, {} if arguments is None else arguments, self.frozen)
        if action == 'phone_observe':
            async def run(command):
                return await self._envelope(command, 60)
            return screen_observation(describe(await observe_phone(run)))
        if action in ACTION_CAPABILITIES:
            envelope = await self._envelope(action_command(action, arguments, self.frozen), 45)
            if envelope.get('exit_code') != 0:
                raise ValueError(action_failure(envelope))
            output = envelope.get('stdout')
            if not isinstance(output, str) or len(output) > 4096:
                raise ValueError('invalid_action_receipt')
            data = json.loads(output)
            if not isinstance(data, dict):
                raise ValueError('invalid_action_receipt')
            return parse_receipt(action, data)
        envelope = await self._envelope(COMMANDS[action], 20)
        if envelope.get('exit_code') != 0:
            raise ValueError('backend_command_failed')
        output = envelope.get('stdout')
        if not isinstance(output, str) or len(output) > 4096:
            raise ValueError('invalid_backend_output')
        data = json.loads(output)
        if not isinstance(data, dict):
            raise ValueError('invalid_observation')
        if action == 'sandbox_status':
            if type(data.get('workspace_exists')) is not bool or any(
                type(data.get(key)) is not int or data[key] < 0 for key in ('free_bytes', 'total_bytes')
            ):
                raise ValueError('invalid_sandbox_status')
            return {key: data[key] for key in ('workspace_exists', 'free_bytes', 'total_bytes')}
        if action == 'phone_app_scope':
            if data.get('scope') not in {'allowed_app', 'blocked_or_unknown'}:
                raise ValueError('invalid_app_scope')
            return {'scope': data['scope'], 'content_read': False}
        if data.get('state') not in {'device', 'offline', 'unknown', 'unavailable', 'client_missing'}:
            raise ValueError('invalid_phone_state')
        return {'state': data['state']}


def planner_tools(actions):
    return ToolSet([
        FunctionTool(
            name=action,
            description='Request the authorized capability with the supplied parameters only.',
            parameters=ACTION_SCHEMAS.get(action, {
                'type': 'object', 'properties': {}, 'additionalProperties': False,
            }),
        ) for action in actions
    ])
