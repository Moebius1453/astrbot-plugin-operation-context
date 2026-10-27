import io
import json
import re
import shlex
import struct
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree


PACKAGE = 'com.max.xiaoheihe'

_ICON_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))
_ZERO_WIDTH = frozenset({0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF})


def normalize_label(value):
    if not isinstance(value, str):
        return ''
    kept = [
        character for character in value
        if ord(character) not in _ZERO_WIDTH
        and not any(low <= ord(character) <= high for low, high in _ICON_RANGES)
    ]
    return ''.join(kept).strip()


@dataclass(frozen=True)
class PhoneNode:
    identifier: str
    class_name: str
    text: str | None
    bounds: tuple[int, int, int, int]
    clickable: bool
    enabled: bool
    desc: str = ''


@dataclass(frozen=True)
class PhoneObservation:
    activity: str
    source: str
    nodes: tuple[PhoneNode, ...]

    def exact_editor(self, identifier: str, expected_text: str) -> PhoneNode:
        node = self.unique(identifier)
        if 'EditText' not in node.class_name or node.text is None or node.text != expected_text:
            raise ValueError('editor_text_not_verified')
        return node

    def unique(self, identifier: str) -> PhoneNode:
        hits = [node for node in self.nodes if node.identifier == identifier]
        if len(hits) != 1:
            raise ValueError('missing_or_ambiguous_control')
        return hits[0]


def foreground(window_dump: str) -> str:
    matches = re.findall(r'^\s*mCurrentFocus=Window\{[^\n]*?\bu\d+\s+([A-Za-z0-9_.]+)/(\.?[A-Za-z0-9_.$]+)\}', window_dump, re.MULTILINE)
    if len(matches) != 1 or matches[0][0] != PACKAGE:
        raise ValueError('phone_outside_allowed_app')
    package, activity = matches[0]
    return package + activity if activity.startswith('.') else activity


def parse_xml(content: str) -> tuple[PhoneNode, ...]:
    if len(content) > 2_000_000 or '<!DOCTYPE' in content or '<!ENTITY' in content:
        raise ValueError('invalid_hierarchy')
    root = ElementTree.fromstring(content)
    if root.tag != 'hierarchy':
        raise ValueError('invalid_hierarchy')
    nodes = []
    for element in root.iter('node'):
        if element.get('package') != PACKAGE:
            raise ValueError('foreign_package_in_hierarchy')
        match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', element.get('bounds', ''))
        if match is None:
            raise ValueError('invalid_node_bounds')
        bounds = tuple(map(int, match.groups()))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            continue
        nodes.append(PhoneNode(
            element.get('resource-id', ''), element.get('class', ''),
            element.get('text', ''), bounds,
            element.get('clickable') == 'true', element.get('enabled') == 'true',
            element.get('content-desc', ''),
        ))
    if not nodes:
        raise ValueError('empty_hierarchy')
    return tuple(nodes)


class _ViewDecoder:
    def __init__(self, data: bytes):
        self.data = data
        self.position = 0
        self.values = 0
        self.conflicts = []

    def take(self, size: int) -> bytes:
        end = self.position + size
        if end > len(self.data):
            raise ValueError('truncated_view_stream')
        value = self.data[self.position:end]
        self.position = end
        return value

    def read(self, depth: int = 0):
        self.values += 1
        if depth > 120 or self.values > 300_000:
            raise ValueError('view_stream_limit')
        kind = self.take(1)
        formats = {b'B': '>b', b'Z': '>?', b'S': '>h', b'I': '>i', b'J': '>q', b'F': '>f', b'D': '>d'}
        if kind in formats:
            fmt = formats[kind]
            return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]
        if kind == b'R':
            length = struct.unpack('>H', self.take(2))[0]
            return self.take(length).decode('utf-8', errors='strict')
        if kind == b'M':
            result = {}
            while True:
                key = self.read(depth + 1)
                if type(key) is not int:
                    raise ValueError('invalid_view_property')
                if key == 0:
                    return result
                value = self.read(depth + 1)
                if key in result and not (
                    type(value) is type(result[key]) and value == result[key]
                    or depth == 0 and isinstance(value, str) and isinstance(result[key], str)
                ):
                    raise ValueError('conflicting_view_property')
                if key in result and value != result[key]:
                    self.conflicts.append((depth, key, result[key], value))
                result[key] = value
        raise ValueError('unsupported_view_type')


def parse_window_views(content: bytes, activity: str) -> tuple[PhoneNode, ...]:
    if len(content) > 4_000_000:
        raise ValueError('view_archive_limit')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        candidates = []
        for item in archive.infolist():
            component = item.filename.split(' ', 1)[-1]
            if component == PACKAGE + '/' + activity:
                candidates.append(item)
        if len(candidates) != 1 or candidates[0].file_size > 4_000_000:
            raise ValueError('missing_or_ambiguous_window')
        data = archive.read(candidates[0])
    decoder = _ViewDecoder(data)
    values = []
    while decoder.position < len(data):
        values.append(decoder.read())
    if len(values) != 6 or any(type(value) is not int for value in values[:4]):
        raise ValueError('invalid_view_header')
    root, names = values[-2:]
    if not isinstance(root, dict) or not isinstance(names, dict):
        raise ValueError('invalid_view_maps')
    if any(type(key) is not int or not isinstance(value, str) for key, value in names.items()):
        raise ValueError('invalid_view_names')
    if any(depth != 0 or old != 'propertyIndex' or new != '__name__' for depth, key, old, new in decoder.conflicts):
        raise ValueError('conflicting_view_names')
    nodes = []

    def walk(raw, x=0, y=0, visible=True, depth=0, clip=None):
        if not isinstance(raw, dict) or depth > 100:
            raise ValueError('invalid_view_tree')
        view = {names.get(key, key): value for key, value in raw.items()}
        visible = visible and view.get('misc:visibility') == 0 and view.get('drawing:alpha', 1) > 0
        x += view.get('layout:left', 0) + view.get('drawing:translationX', 0)
        y += view.get('layout:top', 0) + view.get('drawing:translationY', 0)
        width, height = view.get('layout:width', 0), view.get('layout:height', 0)
        bounds = (x, y, x + width, y + height)
        if clip is not None:
            bounds = (max(bounds[0], clip[0]), max(bounds[1], clip[1]), min(bounds[2], clip[2]), min(bounds[3], clip[3]))
        if visible and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
            identifier = view.get('id', '')
            if identifier.startswith('id/'):
                identifier = PACKAGE + ':' + identifier
            nodes.append(PhoneNode(
                identifier, view.get('meta:__name__', ''), None,
                tuple(round(v) for v in bounds),
                view.get('misc:clickable') is True, view.get('misc:enabled') is True,
            ))
        for key, child in view.items():
            if isinstance(key, str) and key.startswith('meta:__child__'):
                child_clip = bounds if view.get('drawing:clipChildren', True) else clip
                walk(child, x - view.get('scrolling:scrollX', 0), y - view.get('scrolling:scrollY', 0), visible, depth + 1, child_clip)

    if names.get(values[0]) != 'window:left' or names.get(values[2]) != 'window:top':
        raise ValueError('invalid_window_coordinates')
    walk(root, values[1], values[3])
    if not nodes:
        raise ValueError('empty_view_tree')
    return tuple(nodes)


_CAPTURE_SCRIPT = r'''import base64,io,json,re,subprocess,uuid,zipfile
adb=['/workspace/.local/adbdl/platform-tools/adb','-s','172.22.0.2:5555']
def run(args,timeout=12):
    return subprocess.run(adb+args,capture_output=True,timeout=timeout)
def focus():
    r=run(['shell','dumpsys','window'])
    if r.returncode: raise RuntimeError('foreground_query_failed')
    matches=re.findall(rb'^\s*mCurrentFocus=Window\{[^\n]*?\bu\d+\s+([A-Za-z0-9_.]+)/(\.?[A-Za-z0-9_.$]+)\}',r.stdout,re.MULTILINE)
    if len(matches)!=1 or matches[0][0]!=b'com.max.xiaoheihe': raise RuntimeError('phone_outside_allowed_app')
    package,activity=matches[0]
    return 'mCurrentFocus=Window{0 u0 '+package.decode()+'/'+activity.decode()+'}'
before=focus()
path='/sdcard/operation-observe-'+uuid.uuid4().hex+'.xml'
source='uiautomator'
try:
    dump=run(['shell','uiautomator','dump','--compressed',path],18)
    xml=run(['shell','cat',path])
    if dump.returncode or dump.stderr.strip() or xml.returncode or not xml.stdout.lstrip().startswith(b'<?xml'):
        raise RuntimeError('hierarchy_unavailable')
    content=xml.stdout
except (RuntimeError,subprocess.TimeoutExpired):
    source='window_views'
    focus()
    view=run(['exec-out','cmd','window','dump-visible-window-views'])
    if view.returncode or not view.stdout.startswith(b'PK'): raise RuntimeError('window_view_export_failed')
    archive=zipfile.ZipFile(io.BytesIO(view.stdout))
    selected=[entry for entry in archive.infolist() if re.fullmatch(r'[^ ]+ com\.max\.xiaoheihe/[A-Za-z0-9_.$]+',entry.filename)]
    if len(selected)!=1 or selected[0].file_size>4000000: raise RuntimeError('ambiguous_allowed_window')
    filtered=io.BytesIO()
    with zipfile.ZipFile(filtered,'w',compression=zipfile.ZIP_DEFLATED) as target:
        target.writestr(selected[0].filename,archive.read(selected[0]))
    content=filtered.getvalue()
if len(content)>4000000: raise RuntimeError('observation_too_large')
after=focus()
print(json.dumps({'before':before,'after':after,'source':source,'content':base64.b64encode(content).decode('ascii')}))
'''
CAPTURE_COMMAND = 'python3 -I -c ' + shlex.quote(_CAPTURE_SCRIPT)


def describe(observation: PhoneObservation, limit: int = 120) -> dict:
    if type(limit) is not int or limit < 1:
        raise ValueError('invalid_node_limit')
    text_available = observation.source == 'uiautomator'
    meaningful = [
        node for node in observation.nodes
        if node.text or node.identifier or node.clickable or node.desc
    ]
    prefix = PACKAGE + ':id/'
    return {
        'activity': observation.activity,
        'source': observation.source,
        'text_available': text_available,
        'node_count': len(observation.nodes),
        'returned_count': min(len(meaningful), limit),
        'truncated': len(meaningful) > limit,
        'nodes': [
            {
                'id': node.identifier.removeprefix(prefix),
                'class': node.class_name,
                'text': node.text if text_available else None,
                'desc': node.desc,
                'bounds': list(node.bounds),
                'clickable': node.clickable,
                'enabled': node.enabled,
            }
            for node in meaningful[:limit]
        ],
    }


async def observe_phone(execute_command) -> PhoneObservation:
    import base64

    envelope = await execute_command(CAPTURE_COMMAND)
    if not isinstance(envelope, dict) or envelope.get('exit_code') != 0:
        raise ValueError('phone_capture_failed')
    output = envelope.get('stdout')
    if not isinstance(output, str) or len(output) > 6_000_000:
        raise ValueError('invalid_capture_output')
    capture = json.loads(output)
    before, after = foreground(capture['before']), foreground(capture['after'])
    if before != after:
        raise ValueError('activity_changed_during_capture')
    content = base64.b64decode(capture['content'], validate=True)
    if capture['source'] == 'uiautomator':
        nodes = parse_xml(content.decode('utf-8'))
    elif capture['source'] == 'window_views':
        nodes = parse_window_views(content, before)
    else:
        raise ValueError('invalid_observation_source')
    return PhoneObservation(before, capture['source'], nodes)
