import base64
import io
import json
import shlex
from pathlib import Path
from uuid import uuid4

import mcp.types
from PIL import Image

from .task_view import TASK_ID

MAX_BYTES = 8 * 1024 * 1024


class Artifacts:
    def __init__(self, root: Path):
        self.root = Path(root)

    def save_png(self, raw: bytes, umo: str, conversation_id: str | None, source: str) -> str:
        if len(raw) > MAX_BYTES:
            raise ValueError('image_too_large')
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != 'PNG' or image.width * image.height > 20_000_000:
                raise ValueError('invalid_image')
            image.verify()
        artifact_id = uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f'{artifact_id}.png').write_bytes(raw)
        (self.root / f'{artifact_id}.json').write_text(json.dumps({
            'umo': umo, 'conversation_id': conversation_id, 'source': source,
        }, ensure_ascii=False), encoding='utf-8')
        return artifact_id

    def get(self, artifact_id: str, umo: str, conversation_id: str | None) -> Path:
        if not isinstance(artifact_id, str) or not TASK_ID.fullmatch(artifact_id):
            raise ValueError('unknown_artifact')
        path = self.root / f'{artifact_id}.png'
        try:
            meta = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('unknown_artifact') from None
        if meta.get('umo') != umo or meta.get('conversation_id') != conversation_id:
            raise ValueError('unknown_artifact')
        return path


async def capture(shell, run_context, source: str) -> bytes:
    if source == 'phone':
        script = '''import base64,subprocess
adb='/workspace/.local/adbdl/platform-tools/adb'
r=subprocess.run([adb,'-s','172.22.0.2:5555','shell','dumpsys','window'],capture_output=True,text=True,check=True,timeout=10)
lines=[s for s in r.stdout.splitlines() if 'mCurrentFocus=' in s]
if len(lines)!=1 or 'com.max.xiaoheihe/' not in lines[0]:
    raise RuntimeError('phone_outside_allowed_app')
r=subprocess.run([adb,'-s','172.22.0.2:5555','exec-out','screencap','-p'],capture_output=True,check=True,timeout=20)
if len(r.stdout)>8388608: raise RuntimeError('image_too_large')
print(base64.b64encode(r.stdout).decode())'''
    elif source == 'sandbox':
        script = '''import base64,os,shutil,subprocess,tempfile
if not os.path.exists('/tmp/.X11-unix/X99'):
    raise RuntimeError('desktop_not_running')
if shutil.which('scrot') is None:
    raise RuntimeError('desktop_capture_tool_missing')
fd,path=tempfile.mkstemp(suffix='.png');os.close(fd)
try:
    env=dict(os.environ,DISPLAY=':99')
    subprocess.run(['scrot','--overwrite',path],env=env,capture_output=True,check=True,timeout=20)
    with open(path,'rb') as f: raw=f.read(8388609)
    if len(raw)>8388608: raise RuntimeError('image_too_large')
    print(base64.b64encode(raw).decode())
finally:
    os.unlink(path)'''
    else:
        raise ValueError('invalid_capture_source')
    raw = await shell.call(run_context, command='python3 -I -c ' + shlex.quote(script),
                           background=False, timeout=40)
    envelope = json.loads(raw)
    if envelope.get('exit_code') != 0:
        detail = envelope.get('stderr', '')
        for reason in ('phone_outside_allowed_app', 'desktop_not_running', 'desktop_capture_tool_missing'):
            if reason in detail:
                raise ValueError(reason)
        raise ValueError('capture_failed')
    output = envelope.get('stdout', '').strip()
    if len(output) > (MAX_BYTES * 4 // 3 + 8):
        raise ValueError('image_too_large')
    return base64.b64decode(output, validate=True)


class ArtifactTool:
    @staticmethod
    def image_result(artifact_id: str, path: Path, run_context):
        from astrbot.core.agent.message import ImageURLPart, Message
        encoded = base64.b64encode(path.read_bytes()).decode()
        run_context.messages.append(Message(role='user', content=[
            ImageURLPart(image_url=ImageURLPart.ImageURL(
                url='data:image/png;base64,' + encoded,
            )),
        ]))
        return mcp.types.CallToolResult(content=[
            mcp.types.TextContent(type='text', text=json.dumps({
                'artifact_id': artifact_id, 'image_attached': True,
            })),
        ])
