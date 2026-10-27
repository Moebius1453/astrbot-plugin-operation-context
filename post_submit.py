import json
import os
import shlex
import uuid
from pathlib import Path


POST_ACTIVITY = 'com.max.xiaoheihe.module.bbs.post_edit.PostTabActivity'
DETAIL_MARKERS = ('PicturePostPage', 'PostDetail')


def validate_post(title: str, text: str) -> None:
    for value, limit in ((title, 100), (text, 4000)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError('invalid_post_payload')
        if any(ord(character) < 32 and character not in '\n\t' for character in value):
            raise ValueError('invalid_post_payload')


_SCRIPT = r'''import json,re,subprocess,time,uuid,xml.etree.ElementTree as E
payload=json.loads(PAYLOAD)
adb=['/workspace/.local/adbdl/platform-tools/adb','-s','172.22.0.2:5555']
def run(args,timeout=12):
    r=subprocess.run(adb+args,capture_output=True,text=True,timeout=timeout)
    if r.returncode: raise RuntimeError('phone_command_failed')
    return r.stdout
def focus():
    window=run(['shell','dumpsys','window'])
    hits=re.findall(r'^\s*mCurrentFocus=Window\{[^\n]*?\bu\d+\s+([A-Za-z0-9_.]+)/(\.?[A-Za-z0-9_.$]+)\}',window,re.MULTILINE)
    if len(hits)!=1 or hits[0][0]!='com.max.xiaoheihe': raise RuntimeError('phone_outside_allowed_app')
    activity=hits[0][1]
    return activity if not activity.startswith('.') else 'com.max.xiaoheihe'+activity
def controls():
    path='/sdcard/operation-post-'+uuid.uuid4().hex+'.xml'
    r=subprocess.run(adb+['shell','uiautomator','dump','--compressed',path],capture_output=True,text=True,timeout=18)
    if r.returncode or r.stderr.strip(): raise RuntimeError('hierarchy_unavailable')
    root=E.fromstring(run(['shell','cat',path]))
    if any(node.get('package')!='com.max.xiaoheihe' for node in root.iter('node')): raise RuntimeError('foreign_package')
    return root
def tap(node):
    match=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
    if not match: raise RuntimeError('invalid_target_bounds')
    x1,y1,x2,y2=map(int,match.groups())
    if not (0<=x1<x2<=720 and 0<=y1<y2<=1184): raise RuntimeError('unsafe_target_bounds')
    focus()
    run(['shell','input','tap',str((x1+x2)//2),str((y1+y2)//2)])
if focus()!='com.max.xiaoheihe.module.bbs.post_edit.PostTabActivity': raise RuntimeError('editor_required')
root=controls()
for identifier,expected in [('com.max.xiaoheihe:id/et_title',payload['title']),('com.max.xiaoheihe:id/et_content',payload['text'])]:
    nodes=[node for node in root.iter('node') if node.get('resource-id')==identifier]
    if len(nodes)!=1 or nodes[0].get('text')!=expected: raise RuntimeError('editor_content_mismatch')
buttons=[node for node in root.iter('node') if node.get('text')=='发布' and node.get('enabled')=='true']
if len(buttons)!=1: raise RuntimeError('ambiguous_publish_button')
tap(buttons[0])
dialog=False
try:
    after=controls()
    direct=[node for node in after.iter('node') if node.get('text')=='直接发布' and node.get('enabled')=='true']
    if len(direct)==1:
        tap(direct[0])
        dialog=True
except (RuntimeError,subprocess.TimeoutExpired):
    pass
confirmed=False
activity=''
for _ in range(8):
    try:
        activity=focus()
    except RuntimeError:
        activity=''
    if any(marker in activity for marker in ('PicturePostPage','PostDetail')):
        try:
            page=controls()
            titles=[node for node in page.iter('node') if node.get('resource-id')=='com.max.xiaoheihe:id/tv_title']
            bodies=[node for node in page.iter('node') if node.get('resource-id')=='com.max.xiaoheihe:id/tv_desc']
            confirmed=(len(titles)==1 and titles[0].get('text')==payload['title']
                       and len(bodies)==1 and bodies[0].get('text')==payload['text'])
            if confirmed: break
        except (RuntimeError,subprocess.TimeoutExpired):
            pass
    time.sleep(1)
print(json.dumps({'submit_tapped':True,'dialog_tapped':dialog,'activity':activity,'confirmed':confirmed}))
'''


def submit_command(title: str, text: str) -> str:
    validate_post(title, text)
    payload = json.dumps({'title': title, 'text': text}, ensure_ascii=False)
    return 'python3 -I -c ' + shlex.quote(_SCRIPT.replace('PAYLOAD', repr(payload), 1))


def _write(directory: Path, name: str, payload: dict) -> None:
    with (directory / name).open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


class PostSubmitter:
    def __init__(self, execute_command, root: Path):
        self.execute_command = execute_command
        self.root = root

    async def submit(self, title: str, text: str) -> dict:
        validate_post(title, text)
        request = uuid.uuid4().hex
        directory = self.root / request
        directory.mkdir(parents=True, exist_ok=False)
        _write(directory, 'attempt.json', {
            'status': 'unknown', 'request': request, 'title': title, 'text': text,
        })
        try:
            envelope = await self.execute_command(submit_command(title, text))
        except Exception as exc:
            result = {'status': 'unknown', 'error_type': type(exc).__name__, 'object_id_verified': False}
            _write(directory, 'result.json', result)
            return result
        if not isinstance(envelope, dict) or envelope.get('exit_code') != 0:
            result = {'status': 'unknown', 'error_type': 'submit_command_failed', 'object_id_verified': False}
            _write(directory, 'result.json', result)
            return result
        output = envelope.get('stdout')
        try:
            data = json.loads(output) if isinstance(output, str) and len(output) <= 4096 else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or data.get('submit_tapped') is not True:
            result = {'status': 'unknown', 'error_type': 'invalid_submit_receipt', 'object_id_verified': False}
            _write(directory, 'result.json', result)
            return result
        confirmed = data.get('confirmed') is True
        result = {
            'status': 'confirmed' if confirmed else 'unknown',
            'dialog_tapped': data.get('dialog_tapped') is True,
            'activity': data.get('activity') if isinstance(data.get('activity'), str) else None,
            'object_id': None, 'object_id_verified': False,
            'request': request,
        }
        _write(directory, 'result.json', result)
        return result
