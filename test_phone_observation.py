import base64
import io
import json
import struct
import unittest
import zipfile
from unittest.mock import AsyncMock

from .phone_observation import PACKAGE, PhoneNode, PhoneObservation, _ViewDecoder, describe, foreground, observe_phone, parse_window_views, parse_xml


ACTIVITY = PACKAGE + '.module.bbs.post_edit.PostTabActivity'
FOCUS = ' mCurrentFocus=Window{123 u0 ' + PACKAGE + '/' + ACTIVITY + '}'
XML = '<hierarchy><node package="com.max.xiaoheihe" resource-id="com.max.xiaoheihe:id/et_content" class="android.widget.EditText" text="" bounds="[32,245][688,424]" clickable="true" enabled="true"/></hierarchy>'


def encode(value):
    if type(value) is bool:
        return b'Z' + struct.pack('>?', value)
    if type(value) is int:
        return b'I' + struct.pack('>i', value)
    if isinstance(value, str):
        data = value.encode()
        return b'R' + struct.pack('>H', len(data)) + data
    if isinstance(value, dict):
        return b'M' + b''.join(b'S' + struct.pack('>h', key) + encode(item) for key, item in value.items()) + b'S\0\0'
    raise TypeError()


def view_archive(component=PACKAGE + '/' + ACTIVITY, corrupt=False, offset=(0, 0)):
    names = {10: 'window:left', 11: 'window:top', 1: 'meta:__name__', 2: 'id', 3: 'layout:left', 4: 'layout:top', 5: 'layout:width', 6: 'layout:height', 7: 'misc:visibility', 8: 'misc:clickable', 9: 'misc:enabled'}
    node = {1: 'android.widget.EditText', 2: 'id/et_content', 3: 32, 4: 245, 5: 656, 6: 179, 7: 0, 8: True, 9: True}
    data = b''.join(encode(v) for v in (10, offset[0], 11, offset[1], node, names))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('123 ' + component, data[:-1] if corrupt else data)
    return stream.getvalue()


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    def test_foreground_rejects_qq_unknown_and_lookalikes(self):
        self.assertEqual(foreground(FOCUS), ACTIVITY)
        for value in ('', FOCUS.replace(PACKAGE, PACKAGE + '.fake'), FOCUS.replace(PACKAGE, 'com.tencent.mobileqq')):
            with self.assertRaises(ValueError):
                foreground(value)

    def test_xml_empty_editor_is_not_missing_text(self):
        node = parse_xml(XML)[0]
        self.assertEqual(node.text, '')
        self.assertEqual(node.bounds, (32, 245, 688, 424))

    def test_xml_rejects_foreign_package_and_entities(self):
        for value in (XML.replace(PACKAGE, 'com.tencent.mobileqq'), '<!DOCTYPE a>' + XML, '<hierarchy/>'):
            with self.assertRaises(ValueError):
                parse_xml(value)

    def test_window_views_never_claim_text_readback(self):
        node = parse_window_views(view_archive(), ACTIVITY)[0]
        self.assertIsNone(node.text)
        self.assertEqual(node.identifier, PACKAGE + ':id/et_content')
        self.assertEqual(node.bounds, (32, 245, 688, 424))

    def test_structural_fallback_cannot_authorize_editor_submission(self):
        observation = PhoneObservation(ACTIVITY, 'window_views', parse_window_views(view_archive(), ACTIVITY))
        with self.assertRaisesRegex(ValueError, 'editor_text_not_verified'):
            observation.exact_editor(PACKAGE + ':id/et_content', '')

    def test_editor_requires_exact_original_text(self):
        observation = PhoneObservation(ACTIVITY, 'uiautomator', parse_xml(XML))
        observation.exact_editor(PACKAGE + ':id/et_content', '')
        with self.assertRaises(ValueError):
            observation.exact_editor(PACKAGE + ':id/et_content', 'replacement')

    def test_window_coordinates_use_values_not_property_ids(self):
        node = parse_window_views(view_archive(offset=(5, 9)), ACTIVITY)[0]
        self.assertEqual(node.bounds, (37, 254, 693, 433))

    def test_equal_duplicate_properties_are_valid_android_output(self):
        key = b'S' + struct.pack('>h', 17)
        data = b'M' + key + encode(-1) + key + encode(-1) + b'S\0\0'
        self.assertEqual(_ViewDecoder(data).read(), {17: -1})

    def test_conflicting_node_properties_are_rejected(self):
        key = b'S' + struct.pack('>h', 17)
        data = b'M' + key + encode(-1) + key + encode(12) + b'S\0\0'
        with self.assertRaisesRegex(ValueError, 'conflicting_view_property'):
            _ViewDecoder(data).read()

    def test_window_views_reject_wrong_window_and_truncation(self):
        for data in (view_archive('com.tencent.mobileqq/.Main'), view_archive(corrupt=True)):
            with self.assertRaises(ValueError):
                parse_window_views(data, ACTIVITY)

    async def test_activity_switch_rejects_observation(self):
        payload = {'before': FOCUS, 'after': FOCUS.replace(ACTIVITY, PACKAGE + '.MainActivity'), 'source': 'uiautomator', 'content': base64.b64encode(XML.encode()).decode()}
        command = AsyncMock(return_value={'exit_code': 0, 'stdout': json.dumps(payload)})
        with self.assertRaises(ValueError):
            await observe_phone(command)

    async def test_real_schema_preserves_xml_editor_content(self):
        payload = {'before': FOCUS, 'after': FOCUS, 'source': 'uiautomator', 'content': base64.b64encode(XML.encode()).decode()}
        command = AsyncMock(return_value={'exit_code': 0, 'stdout': json.dumps(payload)})
        result = await observe_phone(command)
        self.assertEqual(result.unique(PACKAGE + ':id/et_content').text, '')

    async def test_nonzero_capture_cannot_be_success(self):
        with self.assertRaises(ValueError):
            await observe_phone(AsyncMock(return_value={'exit_code': 1, 'stdout': '{}'}))

    def test_content_desc_is_preserved_and_described(self):
        xml = ('<hierarchy><node package="com.max.xiaoheihe" class="android.view.View" '
               'content-desc="搜索" bounds="[0,0][10,10]" clickable="true" enabled="true"/></hierarchy>')
        node = parse_xml(xml)[0]
        self.assertEqual(node.desc, '搜索')
        self.assertEqual(node.text, '')
        described = describe(PhoneObservation(ACTIVITY, 'uiautomator', (node,)))
        self.assertEqual(described['nodes'][0]['desc'], '搜索')

    def test_describe_keeps_text_only_for_xml_source(self):
        xml_result = describe(PhoneObservation(ACTIVITY, 'uiautomator', parse_xml(XML)))
        self.assertTrue(xml_result['text_available'])
        self.assertEqual(xml_result['nodes'][0]['text'], '')
        self.assertEqual(xml_result['nodes'][0]['id'], 'et_content')
        structural = describe(PhoneObservation(ACTIVITY, 'window_views', parse_window_views(view_archive(), ACTIVITY)))
        self.assertFalse(structural['text_available'])
        self.assertIsNone(structural['nodes'][0]['text'])
        self.assertEqual(structural['nodes'][0]['bounds'], [32, 245, 688, 424])

    def test_describe_drops_decorative_nodes_and_reports_truncation(self):
        nodes = (
            PhoneNode(PACKAGE + ':id/et_title', 'android.widget.EditText', 't', (0, 0, 10, 10), True, True),
            PhoneNode('', 'android.view.View', '', (0, 0, 720, 1184), False, True),
            PhoneNode('', 'android.widget.Button', '', (0, 0, 10, 10), True, True),
        )
        result = describe(PhoneObservation(ACTIVITY, 'uiautomator', nodes))
        self.assertEqual(result['node_count'], 3)
        self.assertEqual(result['returned_count'], 2)
        self.assertFalse(result['truncated'])
        limited = describe(PhoneObservation(ACTIVITY, 'uiautomator', nodes), limit=1)
        self.assertEqual(limited['returned_count'], 1)
        self.assertTrue(limited['truncated'])
