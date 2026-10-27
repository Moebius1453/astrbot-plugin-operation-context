import json
import re
import shlex

from .phone_observation import PACKAGE


POST_ACTIVITY = PACKAGE + '.module.bbs.post_edit.PostTabActivity'

_ADB = "'/workspace/.local/adbdl/platform-tools/adb','-s','172.22.0.2:5555'"

_FOCUS_GUARD = r'''def focus():
    window=run(['shell','dumpsys','window'])
    hits=re.findall(r'^\s*mCurrentFocus=Window\{[^\n]*?\bu\d+\s+([A-Za-z0-9_.]+)/(\.?[A-Za-z0-9_.$]+)\}',window,re.MULTILINE)
    if len(hits)!=1 or hits[0][0]!='com.max.xiaoheihe': raise RuntimeError('phone_outside_allowed_app')
'''

_DUMP_GUARD = r'''def controls():
    path='/sdcard/operation-act-'+uuid.uuid4().hex+'.xml'
    r=subprocess.run(adb+['shell','uiautomator','dump','--compressed',path],capture_output=True,text=True,timeout=18)
    if r.returncode or r.stderr.strip(): raise RuntimeError('hierarchy_unavailable')
    root=E.fromstring(run(['shell','cat',path]))
    if any(node.get('package')!='com.max.xiaoheihe' for node in root.iter('node')): raise RuntimeError('foreign_package')
    return root
'''

_TAP_SCRIPT = r'''import json,re,subprocess,uuid,xml.etree.ElementTree as E
payload=json.loads(PAYLOAD)
adb=[''' + _ADB + r''']
def run(args,timeout=12):
    r=subprocess.run(adb+args,capture_output=True,text=True,timeout=timeout)
    if r.returncode: raise RuntimeError('phone_command_failed')
    return r.stdout
''' + _FOCUS_GUARD + _DUMP_GUARD + r'''focus()
root=controls()
parents={child:parent for parent in root.iter() for child in parent}
if payload['attr']=='text':
    hits=[node for node in root.iter('node') if node.get('text')==payload['target'] or node.get('content-desc')==payload['target']]
else:
    hits=[node for node in root.iter('node') if node.get('resource-id')==payload['target']]
if len(hits)!=1: raise RuntimeError('missing_or_ambiguous_target')
node=hits[0]
while node is not None and node.tag=='node' and node.get('clickable')!='true':
    node=parents.get(node)
if node is None or node.tag!='node' or node.get('clickable')!='true' or node.get('enabled')!='true': raise RuntimeError('target_not_interactive')
match=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
if not match: raise RuntimeError('invalid_target_bounds')
x1,y1,x2,y2=map(int,match.groups())
if not (0<=x1<x2<=720 and 0<=y1<y2<=1184): raise RuntimeError('unsafe_target_bounds')
focus()
run(['shell','input','tap',str((x1+x2)//2),str((y1+y2)//2)])
print(json.dumps({'tap_attempted':True,'bounds':[x1,y1,x2,y2]}))
'''

_BACK_SCRIPT = r'''import json,re,subprocess
adb=[''' + _ADB + r''']
def run(args,timeout=12):
    r=subprocess.run(adb+args,capture_output=True,text=True,timeout=timeout)
    if r.returncode: raise RuntimeError('phone_command_failed')
    return r.stdout
''' + _FOCUS_GUARD + r'''focus()
run(['shell','input','keyevent','KEYCODE_BACK'])
print(json.dumps({'back_attempted':True}))
'''

_START_SCRIPT = r'''import json,re,subprocess
adb=[''' + _ADB + r''']
def run(args,timeout=12):
    r=subprocess.run(adb+args,capture_output=True,text=True,timeout=timeout)
    if r.returncode: raise RuntimeError('phone_command_failed')
    return r.stdout
run(['shell','am','start','-n','com.max.xiaoheihe/.MainActivity'])
print(json.dumps({'start_attempted':True}))
'''

_INPUT_SCRIPT = r'''import base64,json,re,subprocess,uuid,xml.etree.ElementTree as E
payload=json.loads(PAYLOAD)
adb=[''' + _ADB + r''']
def run(args,timeout=12):
    r=subprocess.run(adb+args,capture_output=True,text=True,timeout=timeout)
    if r.returncode: raise RuntimeError('phone_command_failed')
    return r.stdout
''' + _FOCUS_GUARD + _DUMP_GUARD + r'''focus()
if run(['shell','settings','get','secure','default_input_method']).strip()!='com.android.adbkeyboard/.AdbIME': raise RuntimeError('trusted_input_method_required')
root=controls()
hits=[node for node in root.iter('node') if node.get('resource-id')==payload['target']]
if len(hits)!=1: raise RuntimeError('missing_or_ambiguous_target')
node=hits[0]
if 'EditText' not in node.get('class','') or node.get('enabled')!='true' or node.get('password')=='true': raise RuntimeError('target_not_editable')
match=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
if not match: raise RuntimeError('invalid_target_bounds')
x1,y1,x2,y2=map(int,match.groups())
if not (0<=x1<x2<=720 and 0<=y1<y2<=1184): raise RuntimeError('unsafe_target_bounds')
focus()
run(['shell','input','tap',str((x1+x2)//2),str((y1+y2)//2)])
focus()
encoded=base64.b64encode(payload['text'].encode('utf-8')).decode('ascii')
run(['shell','am','broadcast','-a','ADB_INPUT_B64','-p','com.android.adbkeyboard','--es','msg',encoded])
verified=None
try:
    after=controls()
    hits=[node for node in after.iter('node') if node.get('resource-id')==payload['target']]
    if len(hits)==1: verified=hits[0].get('text')==payload['text']
except Exception:
    verified=None
print(json.dumps({'input_attempted':True,'text_verified':verified}))
'''

_HOME_SCRIPT = r'''import json,subprocess
adb=[''' + _ADB + r''']
subprocess.run(adb+['shell','input','keyevent','KEYCODE_HOME'],capture_output=True,text=True,timeout=12)
print(json.dumps({'home_attempted':True}))
'''

_SWIPE_SCRIPT = r'''import json,re,subprocess,uuid,xml.etree.ElementTree as E
payload=json.loads(PAYLOAD)
adb=[''' + _ADB + r''']
def run(args,timeout=12):
    r=subprocess.run(adb+args,capture_output=True,text=True,timeout=timeout)
    if r.returncode: raise RuntimeError('phone_command_failed')
    return r.stdout
''' + _FOCUS_GUARD + _DUMP_GUARD + r'''focus()
root=controls()
hits=[node for node in root.iter('node') if node.get('resource-id')==payload['target']]
if len(hits)!=1: raise RuntimeError('missing_or_ambiguous_target')
node=hits[0]
match=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
if not match: raise RuntimeError('invalid_target_bounds')
x1,y1,x2,y2=map(int,match.groups())
if not (0<=x1<x2<=720 and 0<=y1<y2<=1184): raise RuntimeError('unsafe_target_bounds')
cx,cy=(x1+x2)//2,(y1+y2)//2
if payload['direction']=='up': args=[cx,y1+(y2-y1)*7//10,cx,y1+(y2-y1)*3//10]
elif payload['direction']=='down': args=[cx,y1+(y2-y1)*3//10,cx,y1+(y2-y1)*7//10]
elif payload['direction']=='left': args=[x1+(x2-x1)*7//10,cy,x1+(x2-x1)*3//10,cy]
else: args=[x1+(x2-x1)*3//10,cy,x1+(x2-x1)*7//10,cy]
focus()
run(['shell','input','swipe']+[str(value) for value in args]+['300'])
print(json.dumps({'swipe_attempted':True,'bounds':[x1,y1,x2,y2]}))
'''

_SCREENSHOT_SCRIPT = r'''import base64,json,subprocess,uuid
adb=[''' + _ADB + r''']
path='/workspace/phone-shot-'+uuid.uuid4().hex+'.png'
r=subprocess.run(adb+['exec-out','screencap','-p'],capture_output=True,timeout=20)
if r.returncode or not r.stdout.startswith(b'\x89PNG'): raise RuntimeError('screenshot_failed')
open(path,'wb').write(r.stdout)
print(json.dumps({'screenshot':path,'bytes':len(r.stdout),'image':'data:image/png;base64,'+base64.b64encode(r.stdout).decode('ascii')}))
'''

_SAFE_ID = re.compile(r'^[A-Za-z0-9_.]+$')
_DIRECTIONS = ('up', 'down', 'left', 'right')
_SCRIPTS = {
    'phone_tap': _TAP_SCRIPT,
    'phone_back': _BACK_SCRIPT,
    'phone_home': _HOME_SCRIPT,
    'phone_start_app': _START_SCRIPT,
    'phone_swipe': _SWIPE_SCRIPT,
    'phone_screenshot': _SCREENSHOT_SCRIPT,
    'phone_input': _INPUT_SCRIPT,
}


def _resource_id(target: str) -> str:
    if not _SAFE_ID.fullmatch(target):
        raise ValueError('invalid_resource_id')
    return target if target.startswith(PACKAGE + ':id/') else PACKAGE + ':id/' + target


def action_command(action: str, arguments: dict, frozen: dict) -> str:
    script = _SCRIPTS[action]
    if action == 'phone_tap':
        match = arguments.get('match', 'id')
        target = _resource_id(arguments['target']) if match == 'id' else arguments['target']
        payload = json.dumps({'attr': 'resource-id' if match == 'id' else 'text', 'target': target}, ensure_ascii=False)
    elif action == 'phone_input':
        payload = json.dumps({'target': _resource_id(arguments['target']), 'text': frozen['text']}, ensure_ascii=False)
    elif action == 'phone_swipe':
        payload = json.dumps({'target': _resource_id(arguments['target']), 'direction': arguments['direction']}, ensure_ascii=False)
    else:
        payload = '{}'
    return 'python3 -I -c ' + shlex.quote(script.replace('PAYLOAD', repr(payload), 1))


def parse_receipt(action: str, data: dict) -> dict:
    if action == 'phone_tap':
        bounds = data.get('bounds')
        if data.get('tap_attempted') is not True or not (
            isinstance(bounds, list) and len(bounds) == 4 and all(type(value) is int for value in bounds)
        ):
            raise ValueError('invalid_tap_receipt')
        return {'action': 'tap', 'bounds': bounds}
    if action == 'phone_swipe':
        bounds = data.get('bounds')
        if data.get('swipe_attempted') is not True or not (
            isinstance(bounds, list) and len(bounds) == 4 and all(type(value) is int for value in bounds)
        ):
            raise ValueError('invalid_swipe_receipt')
        return {'action': 'swipe', 'bounds': bounds}
    if action == 'phone_back':
        if data.get('back_attempted') is not True:
            raise ValueError('invalid_back_receipt')
        return {'action': 'back'}
    if action == 'phone_home':
        if data.get('home_attempted') is not True:
            raise ValueError('invalid_home_receipt')
        return {'action': 'home'}
    if action == 'phone_start_app':
        if data.get('start_attempted') is not True:
            raise ValueError('invalid_start_receipt')
        return {'action': 'start_app', 'confirmed_by_observation': False}
    if action == 'phone_screenshot':
        path, size = data.get('screenshot'), data.get('bytes')
        if not isinstance(path, str) or not re.fullmatch(r'/workspace/phone-shot-[0-9a-f]{32}\.png', path):
            raise ValueError('invalid_screenshot_receipt')
        if type(size) is not int or size <= 0:
            raise ValueError('invalid_screenshot_receipt')
        return {'action': 'screenshot', 'path': path, 'bytes': size}
    verified = data.get('text_verified')
    if data.get('input_attempted') is not True or verified not in (True, False, None):
        raise ValueError('invalid_input_receipt')
    if verified is True:
        return {'action': 'input', 'state': 'applied', 'text_verified': True}
    return {'action': 'input', 'state': 'unknown', 'text_verified': None}
