import json
import shlex

from .phone_observation import PACKAGE, observe_phone
from .publication import Publication


POST_ACTIVITY = PACKAGE + '.module.bbs.post_edit.PostTabActivity'
EDITORS = {'title': PACKAGE + ':id/et_title', 'content': PACKAGE + ':id/et_content'}


_INPUT_SCRIPT = r'''import base64,json,re,subprocess,uuid,xml.etree.ElementTree as E
payload=json.loads(PAYLOAD)
adb=['/workspace/.local/adbdl/platform-tools/adb','-s','172.22.0.2:5555']
def run(args,timeout=12):
    result=subprocess.run(adb+args,capture_output=True,text=True,timeout=timeout)
    if result.returncode: raise RuntimeError('phone_command_failed')
    return result.stdout
def check_focus():
    window=run(['shell','dumpsys','window'])
    focus=re.findall(r'^\s*mCurrentFocus=Window\{[^\n]*?\bu\d+\s+([A-Za-z0-9_.]+)/(\.?[A-Za-z0-9_.$]+)\}',window,re.MULTILINE)
    if focus!=[('com.max.xiaoheihe','com.max.xiaoheihe.module.bbs.post_edit.PostTabActivity')]: raise RuntimeError('editor_focus_changed')
check_focus()
if run(['shell','settings','get','secure','default_input_method']).strip()!='com.android.adbkeyboard/.AdbIME': raise RuntimeError('trusted_input_method_required')
path='/sdcard/operation-input-'+uuid.uuid4().hex+'.xml'
result=subprocess.run(adb+['shell','uiautomator','dump','--compressed',path],capture_output=True,text=True,timeout=18)
if result.returncode or result.stderr.strip(): raise RuntimeError('editor_tree_unavailable')
root=E.fromstring(run(['shell','cat',path]))
if any(node.get('package')!='com.max.xiaoheihe' for node in root.iter('node')): raise RuntimeError('foreign_package')
hits=[node for node in root.iter('node') if node.get('resource-id')==payload['identifier']]
if len(hits)!=1: raise RuntimeError('ambiguous_editor')
node=hits[0]
if 'EditText' not in node.get('class','') or node.get('text')!=payload['expected']: raise RuntimeError('existing_editor_content_changed')
if node.get('enabled')!='true' or node.get('password')=='true': raise RuntimeError('editor_not_editable')
match=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
if match is None: raise RuntimeError('invalid_editor_bounds')
x1,y1,x2,y2=map(int,match.groups())
if not (0<=x1<x2<=720 and 0<=y1<y2<=1184): raise RuntimeError('unsafe_editor_bounds')
check_focus()
run(['shell','input','tap',str((x1+x2)//2),str((y1+y2)//2)])
check_focus()
encoded=base64.b64encode(payload['text'].encode('utf-8')).decode('ascii')
result=run(['shell','am','broadcast','-a','ADB_INPUT_B64','-p','com.android.adbkeyboard','--es','msg',encoded])
print(json.dumps({'input_attempted':True,'publication_attempted':False}))
'''


def input_command(field: str, expected: str, text: str) -> str:
    if field not in EDITORS or not isinstance(expected, str):
        raise ValueError('invalid_editor_field')
    Publication('post', 'own_account', text, text).validate()
    if expected not in {'填写标题', '添加正文', ''}:
        raise ValueError('existing_content_must_not_be_replaced')
    payload = json.dumps({'identifier': EDITORS[field], 'expected': expected, 'text': text}, ensure_ascii=False)
    script = _INPUT_SCRIPT.replace('PAYLOAD', repr(payload), 1)
    return 'python3 -I -c ' + shlex.quote(script)


async def prepare_test_post(payload: Publication, execute_command) -> dict:
    payload.validate()
    if payload.action != 'post':
        raise ValueError('post_required')
    observation = await observe_phone(execute_command)
    if observation.activity != POST_ACTIVITY:
        raise ValueError('post_editor_required')
    fields = (('title', payload.title, '填写标题'), ('content', payload.text, '添加正文'))
    for field, desired, placeholder in fields:
        node = observation.unique(EDITORS[field])
        if node.text not in ('', placeholder, desired):
            raise ValueError('existing_draft_must_not_be_changed')
    for field, desired, placeholder in fields:
        node = observation.unique(EDITORS[field])
        if node.text == desired:
            observation.exact_editor(EDITORS[field], desired)
            continue
        if node.text not in ('', placeholder):
            raise ValueError('existing_draft_must_not_be_changed')
        envelope = await execute_command(input_command(field, node.text, desired))
        if not isinstance(envelope, dict) or envelope.get('exit_code') != 0:
            raise ValueError('input_failed_no_retry')
        result = json.loads(envelope.get('stdout', ''))
        if result != {'input_attempted': True, 'publication_attempted': False}:
            raise ValueError('invalid_input_result')
        observation = await observe_phone(execute_command)
        if observation.activity != POST_ACTIVITY:
            raise ValueError('editor_changed_after_input')
        observation.exact_editor(EDITORS[field], desired)
    observation = await observe_phone(execute_command)
    if observation.activity != POST_ACTIVITY:
        raise ValueError('editor_changed_before_verification')
    observation.exact_editor(EDITORS['title'], payload.title)
    observation.exact_editor(EDITORS['content'], payload.text)
    return {'target': payload.target, 'text': payload.text, 'title': payload.title}
