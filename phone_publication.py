import json
import shlex

from .phone_editor import prepare_test_post
from .phone_observation import PACKAGE, observe_phone


_SUBMIT_SCRIPT = r'''import json,re,subprocess,uuid,xml.etree.ElementTree as E
payload=json.loads(PAYLOAD)
adb=['/workspace/.local/adbdl/platform-tools/adb','-s','172.22.0.2:5555']
def run(args,timeout=12):
    r=subprocess.run(adb+args,capture_output=True,text=True,timeout=timeout)
    if r.returncode: raise RuntimeError('phone_command_failed')
    return r.stdout
def focus():
    w=run(['shell','dumpsys','window'])
    hits=re.findall(r'^\s*mCurrentFocus=Window\{[^\n]*?\bu\d+\s+([A-Za-z0-9_.]+)/(\.?[A-Za-z0-9_.$]+)\}',w,re.MULTILINE)
    if hits!=[('com.max.xiaoheihe','com.max.xiaoheihe.module.bbs.post_edit.PostTabActivity')]: raise RuntimeError('unexpected_editor_focus')
focus()
p='/sdcard/operation-submit-'+uuid.uuid4().hex+'.xml'
r=subprocess.run(adb+['shell','uiautomator','dump','--compressed',p],capture_output=True,text=True,timeout=18)
if r.returncode or r.stderr.strip(): raise RuntimeError('editor_not_observable')
root=E.fromstring(run(['shell','cat',p]))
if any(n.get('package')!='com.max.xiaoheihe' for n in root.iter('node')): raise RuntimeError('foreign_package')
for identifier,text in [('et_title',payload['title']),('et_content',payload['text'])]:
    nodes=[n for n in root.iter('node') if n.get('resource-id')=='com.max.xiaoheihe:id/'+identifier]
    if len(nodes)!=1 or nodes[0].get('text')!=text: raise RuntimeError('editor_changed')
buttons=[n for n in root.iter('node') if n.get('text')=='发布' and n.get('enabled')=='true']
if len(buttons)!=1: raise RuntimeError('ambiguous_publish_button')
match=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',buttons[0].get('bounds',''))
if not match: raise RuntimeError('invalid_publish_bounds')
x1,y1,x2,y2=map(int,match.groups())
if not (0<=x1<x2<=720 and 0<=y1<y2<=1184): raise RuntimeError('unsafe_publish_bounds')
focus()
run(['shell','input','tap',str((x1+x2)//2),str((y1+y2)//2)])
print(json.dumps({'submit_tapped':True}))
'''


class PhonePostDriver:
    def __init__(self, execute_command, evidence):
        self.execute_command = execute_command
        self.evidence = evidence

    async def prepare(self, payload):
        return await prepare_test_post(payload, self.execute_command)

    async def submit(self, payload):
        payload.validate()
        if payload.action != 'post':
            raise ValueError('post_only')
        script = _SUBMIT_SCRIPT.replace('PAYLOAD', repr(json.dumps({'title': payload.title, 'text': payload.text}, ensure_ascii=False)), 1)
        envelope = await self.execute_command('python3 -I -c ' + shlex.quote(script))
        if envelope.get('exit_code') != 0:
            raise ValueError('submit_failed_status_unknown')
        result = json.loads(envelope.get('stdout', ''))
        if result != {'submit_tapped': True}:
            raise ValueError('invalid_submit_receipt')
        return result

    async def read_back(self, payload, receipt):
        observation = await observe_phone(self.execute_command)
        self.evidence({
            'stage': 'post_submit_observation', 'activity': observation.activity,
            'source': observation.source,
            'nodes': [{'id': n.identifier, 'text': n.text, 'bounds': n.bounds} for n in observation.nodes],
            'publication_status': 'unknown_until_object_verified',
        })
        return None
