"""Read and write the notes and diary that live in her workspace.

These are her own files, so their text is allowed in the main conversation.
Writes never overwrite silently: replace keeps a timestamped backup first,
and create refuses to clobber an existing file.
"""

import base64
import json
import re
import shlex
import time

ROOT = '/workspace/shiro'
ALLOWED_DIRS = ('notes', 'diary')
MAX_READ_CHARS = 20000
MAX_WRITE_CHARS = 20000
MODES = ('create', 'append', 'replace')

_SAFE_NAME = re.compile(r'^[^/\\:*?"<>|\x00-\x1f]+\.md$')


def validate_note_path(path):
    if not isinstance(path, str) or not path.strip():
        raise ValueError('invalid_note_path')
    cleaned = path.strip().lstrip('/')
    parts = cleaned.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('invalid_note_path')
    if parts[0] not in ALLOWED_DIRS or len(parts) < 2:
        raise ValueError('note_path_outside_allowed_dirs')
    if not _SAFE_NAME.fullmatch(parts[-1]):
        raise ValueError('invalid_note_path')
    return ROOT + '/' + cleaned


def validate_mode(mode):
    if mode not in MODES:
        raise ValueError('invalid_note_mode')
    return mode


def validate_content(text):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_WRITE_CHARS:
        raise ValueError('invalid_note_content')
    return text


_READ_SCRIPT = r'''import base64,json,os
payload=json.loads(PAYLOAD)
path=payload['path']
if not os.path.isfile(path): raise RuntimeError('note_missing')
with open(path,'rb') as f: data=f.read()
print(json.dumps({'content':base64.b64encode(data).decode('ascii'),'bytes':len(data)}))
'''


_WRITE_SCRIPT = r'''import base64,json,os,time
payload=json.loads(PAYLOAD)
path=payload['path']
mode=payload['mode']
data=base64.b64decode(payload['content'])
directory=os.path.dirname(path)
os.makedirs(directory,exist_ok=True)
if not os.path.abspath(directory).startswith('/workspace/shiro/'): raise RuntimeError('note_path_outside_workspace')
exists=os.path.isfile(path)
backup=None
if mode=='create' and exists: raise RuntimeError('note_already_exists')
if mode=='replace' and exists:
    backup=path+'.bak-'+time.strftime('%Y%m%d-%H%M%S')
    with open(path,'rb') as src, open(backup,'wb') as dst: dst.write(src.read())
if mode=='append' and exists:
    with open(path,'ab') as f: f.write(data)
else:
    temporary=path+'.tmp-'+str(os.getpid())
    with open(temporary,'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary,path)
with open(path,'rb') as f: written=len(f.read())
print(json.dumps({'written':True,'bytes':written,'backup':backup,'mode':mode}))
'''


class NoteStore:
    def __init__(self, execute_command):
        self.execute_command = execute_command

    async def read(self, path):
        target = validate_note_path(path)
        script = _READ_SCRIPT.replace('PAYLOAD', repr(json.dumps({'path': target})), 1)
        envelope = await self.execute_command('python3 -I -c ' + shlex.quote(script))
        if not isinstance(envelope, dict) or envelope.get('exit_code') != 0:
            detail = envelope.get('stderr') if isinstance(envelope, dict) else None
            raise ValueError('note_read_failed: ' + (detail or '')[-200:])
        data = json.loads(envelope.get('stdout') or '{}')
        content = base64.b64decode(data['content']).decode('utf-8', 'replace')
        truncated = len(content) > MAX_READ_CHARS
        return {
            'path': target, 'content': content[:MAX_READ_CHARS],
            'bytes': data.get('bytes'), 'truncated': truncated,
        }

    async def write(self, path, mode, text):
        target = validate_note_path(path)
        validate_mode(mode)
        validate_content(text)
        payload = json.dumps({
            'path': target, 'mode': mode,
            'content': base64.b64encode(text.encode('utf-8')).decode('ascii'),
        })
        script = _WRITE_SCRIPT.replace('PAYLOAD', repr(payload), 1)
        envelope = await self.execute_command('python3 -I -c ' + shlex.quote(script))
        if not isinstance(envelope, dict) or envelope.get('exit_code') != 0:
            detail = envelope.get('stderr') if isinstance(envelope, dict) else None
            raise ValueError('note_write_failed: ' + (detail or '')[-200:])
        return json.loads(envelope.get('stdout') or '{}')
