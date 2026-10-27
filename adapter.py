import json
import shlex

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor


TOOL_NAME = 'operation_context_run'
SNAPSHOT_KEY = 'operation_context_authorized_tools'
CAPABILITIES = frozenset({'sandbox_status', 'phone_status', 'phone_app_scope'})
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
    if not isinstance(actions, list) or not actions or len(actions) > 2:
        raise ValueError('actions_must_be_nonempty_list_up_to_two')
    if not isinstance(configured, list) or not all(isinstance(x, str) for x in configured):
        raise ValueError('invalid_capability_configuration')
    if not all(isinstance(x, str) and x in CAPABILITIES and x in configured for x in actions):
        raise ValueError('capability_not_authorized')
    if len(set(actions)) != len(actions):
        raise ValueError('duplicate_action')
    return tuple(actions)


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
    def __init__(self, shell, run_context):
        self.shell = shell
        self.run_context = run_context

    async def execute(self, action):
        if action not in CAPABILITIES:
            raise ValueError('unsupported_capability')
        raw = await self.shell.call(
            self.run_context, command=COMMANDS[action], background=False, timeout=20,
        )
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or envelope.get('exit_code') != 0:
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
        FunctionTool(name=action, description='Request the authorized status observation.', parameters={
            'type': 'object', 'properties': {}, 'additionalProperties': False,
        }) for action in actions
    ])
