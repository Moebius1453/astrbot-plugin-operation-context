import json
import os
import shlex
import uuid
from pathlib import Path


DETAIL_MARKERS = ('PicturePostPage', 'PostDetail')
MAX_COMMENT_CHARS = 1000
EDITOR_ID = 'com.max.xiaoheihe:id/et_editor'
SEND_LABELS = ('发送',)


def validate_comment(text: str) -> None:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_COMMENT_CHARS:
        raise ValueError('invalid_comment_text')
    if any(ord(character) < 32 and character not in '\n\t' for character in text):
        raise ValueError('invalid_comment_text')


def _build_script(text: str) -> str:
    payload = json.dumps({'text': text}, ensure_ascii=False)
    return _SCRIPT.replace('PAYLOAD', repr(payload), 1)


def comment_command(text: str) -> str:
    validate_comment(text)
    return 'python3 -I -c ' + shlex.quote(_build_script(text))


_SCRIPT = r'''import base64,json,re,subprocess,time,uuid,xml.etree.ElementTree as E
payload=json.loads(PAYLOAD)
adb=['/workspace/.local/adbdl/platform-tools/adb','-s','172.22.0.2:5555']
def run(args,timeout=12):
    result=subprocess.run(adb+args,capture_output=True,text=True,timeout=timeout)
    if result.returncode: raise RuntimeError('phone_command_failed')
    return result.stdout
def focus():
    window=run(['shell','dumpsys','window'])
    hits=re.findall(r'^\s*mCurrentFocus=Window\{[^\n]*?\bu\d+\s+([A-Za-z0-9_.]+)/(\.?[A-Za-z0-9_.$]+)\}',window,re.MULTILINE)
    if len(hits)!=1 or hits[0][0]!='com.max.xiaoheihe': raise RuntimeError('phone_outside_allowed_app')
    activity=hits[0][1]
    return activity if not activity.startswith('.') else 'com.max.xiaoheihe'+activity
def controls():
    path='/sdcard/operation-comment-'+uuid.uuid4().hex+'.xml'
    result=subprocess.run(adb+['shell','uiautomator','dump','--compressed',path],capture_output=True,text=True,timeout=18)
    if result.returncode or result.stderr.strip(): raise RuntimeError('hierarchy_unavailable')
    root=E.fromstring(run(['shell','cat',path]))
    if any(node.get('package')!='com.max.xiaoheihe' for node in root.iter('node')): raise RuntimeError('foreign_package')
    return root
def tap(node):
    match=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
    if not match: raise RuntimeError('invalid_target_bounds')
    x1,y1,x2,y2=map(int,match.groups())
    if not (0<=x1<x2<=720 and 0<=y1<y2<=1184): raise RuntimeError('unsafe_target_bounds')
    run(['shell','input','tap',str((x1+x2)//2),str((y1+y2)//2)])
def norm(value):
    kept=[ch for ch in (value or '') if not (0xE000<=ord(ch)<=0xF8FF or 0xF0000<=ord(ch)<=0xFFFFD or 0x100000<=ord(ch)<=0x10FFFD or ord(ch) in (0x200B,0x200C,0x200D,0x200E,0x200F,0xFEFF))]
    return ''.join(kept).strip()
desired=payload['text']
activity=focus()
if not any(marker in activity for marker in ('PicturePostPage','PostDetail')): raise RuntimeError('post_detail_required')
root=controls()
editors=[node for node in root.iter('node') if node.get('resource-id')=='com.max.xiaoheihe:id/et_editor']
if len(editors)!=1: raise RuntimeError('comment_box_missing')
current=norm(editors[0].get('text'))
if desired not in current: raise RuntimeError('comment_content_mismatch')
if len(current)>len(desired)+64: raise RuntimeError('comment_content_mismatch')
sends=[node for node in root.iter('node') if norm(node.get('text')) in ('发送',) and node.get('enabled')=='true']
if len(sends)!=1: raise RuntimeError('send_control_missing')
tap(sends[0])
probe=desired[:20]
confirmed=False
for _ in range(8):
    time.sleep(1)
    try:
        page=controls()
    except (RuntimeError,subprocess.TimeoutExpired):
        continue
    texts=[norm(node.get('text')) for node in page.iter('node')]
    if any(probe and probe in text for text in texts):
        confirmed=True
        break
print(json.dumps({'submit_tapped':True,'confirmed':confirmed}))
'''


def _write(directory: Path, name: str, payload: dict) -> None:
    with (directory / name).open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


class CommentSubmitter:
    def __init__(self, execute_command, root: Path):
        self.execute_command = execute_command
        self.root = root

    async def submit(self, text: str) -> dict:
        validate_comment(text)
        request = uuid.uuid4().hex
        directory = self.root / request
        directory.mkdir(parents=True, exist_ok=False)
        _write(directory, 'attempt.json', {'status': 'unknown', 'request': request, 'text': text})
        try:
            envelope = await self.execute_command(comment_command(text))
        except Exception as exc:
            result = {'status': 'unknown', 'error_type': type(exc).__name__}
            _write(directory, 'result.json', result)
            return result
        if not isinstance(envelope, dict) or envelope.get('exit_code') != 0:
            detail = envelope.get('stderr') if isinstance(envelope, dict) else None
            result = {
                'status': 'unknown', 'error_type': 'submit_command_failed',
                'exit_code': envelope.get('exit_code') if isinstance(envelope, dict) else None,
                'stderr': detail[-400:] if isinstance(detail, str) else None,
            }
            _write(directory, 'result.json', result)
            return result
        output = envelope.get('stdout')
        try:
            data = json.loads(output) if isinstance(output, str) and len(output) <= 4096 else None
        except ValueError:
            data = None
        if not isinstance(data, dict) or data.get('submit_tapped') is not True:
            result = {'status': 'unknown', 'error_type': 'invalid_submit_receipt'}
            _write(directory, 'result.json', result)
            return result
        result = {
            'status': 'confirmed' if data.get('confirmed') is True else 'unknown',
            'request': request,
        }
        _write(directory, 'result.json', result)
        return result
