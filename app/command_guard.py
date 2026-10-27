"""Command-level guard for the free-execution path.

The executor on that path holds a real shell, so the tool schema is not a
boundary: nothing stops `pm clear` or a broadcast to a foreign package. This
module is the choke point every model-issued command passes before it reaches
the shell. It answers with a reason string, or None when the command may run.

The guard is deliberately a blocklist. It cannot prove a command is safe, it
only refuses a small set that would destroy state the task does not own. A
command it lets through is still the model's responsibility.
"""

import re

PHONE_SERIAL = '172.22.0.2:5555'

QQ_PACKAGES = ('com.tencent.mobileqq', 'com.tencent.tim', 'com.tencent.qqlite')

# Generic blocklist: (pattern, reason). Checked after the specific ones below.
BLOCKED = (
    (re.compile(r'\bpm\s+(?:clear|uninstall|install|disable|enable|hide|unhide|set-installer)\b'),
     'package_mutation'),
    (re.compile(r'\badb\b[^|;&\n]*?\buninstall\b'), 'package_mutation'),
    (re.compile(r'\bsettings\s+(?:put|delete|reset)\b'), 'system_setting_mutation'),
    (re.compile(r'\bwm\s+(?:size|density)\s+\d'), 'display_mutation'),
    (re.compile(r'\bsvc\s+(?:wifi|data|bluetooth)\s+(?:enable|disable)\b'), 'radio_mutation'),
    (re.compile(r'\breboot\b|\bshutdown\b'), 'device_power'),
    (re.compile(r'\bdd\s+if='), 'disk_mutation'),
    (re.compile(r'\bmkfs\b'), 'disk_mutation'),
    (re.compile(r'\b(?:pip|pip3|apt|apt-get|npm|yarn|conda)\s+(?:install|add)\b'), 'package_install'),
    (re.compile(r'\b(?:systemctl|crontab|nohup|setsid)\b'), 'background_process'),
    (re.compile(r'\bservice\s+\S+\s+(?:start|restart)\b'), 'background_process'),
    (re.compile(r'\bpush\b[^|;&\n]*\s/(?:system|vendor|data/app)\b'), 'system_partition_write'),
)

_BROADCAST = re.compile(r'\bam\s+broadcast\b')
_ACTION_ARG = re.compile(r'(?:-a|--action)[=\s]+(\S+)')
_SERIAL_ARG = re.compile(r'\badb\b[^|;&\n]*?\s-s\s+([^\s|;&]+)')
_RECURSIVE_RM = re.compile(r'\brm\b([^|;&\n]*)')
_DEVICE_ACTION = re.compile(r'\binput\s+(?:tap|swipe|text|keyevent|drag)\b|\bam\s+broadcast\b')


def _check_broadcast(command):
    if not _BROADCAST.search(command):
        return None
    actions = _ACTION_ARG.findall(command)
    if not actions:
        return 'broadcast_without_explicit_action'
    for action in actions:
        if action.strip('\'"') != 'ADB_INPUT_B64':
            return 'broadcast_not_input'
    return None


def _check_serial(command):
    for serial in _SERIAL_ARG.findall(command):
        if serial.strip('\'"') != PHONE_SERIAL:
            return 'foreign_device'
    return None


def _check_recursive_delete(command):
    for arguments in _RECURSIVE_RM.findall(command):
        words = arguments.split()
        flags = [word for word in words if word.startswith('-')]
        if not any('r' in flag.lower() for flag in flags):
            continue
        targets = [word for word in words if not word.startswith('-')]
        if any(not target.strip('\'"').startswith('/tmp') for target in targets):
            return 'recursive_delete_outside_tmp'
    return None


def _check_qq(command):
    if any(package in command for package in QQ_PACKAGES):
        return 'qq_forbidden'
    return None


def guard(command):
    """Return a reason string when the command must not run, else None."""
    if not isinstance(command, str):
        return 'invalid_command'
    for check in (_check_qq, _check_serial, _check_broadcast, _check_recursive_delete):
        reason = check(command)
        if reason:
            return reason
    for pattern, reason in BLOCKED:
        if pattern.search(command):
            return reason
    return None


def count_device_actions(command):
    """Count commands that move the phone, for the evidence record."""
    if not isinstance(command, str):
        return 0
    return len(_DEVICE_ACTION.findall(command))
