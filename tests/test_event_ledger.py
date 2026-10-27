import tempfile
import unittest
from pathlib import Path

from ..app.event_ledger import EventLedger, classify, fingerprint, parse_notifications, to_events

DUMP = '''Current Notification Manager state:
  Notification List:
    NotificationRecord(0x062a379d: pkg=com.max.xiaoheihe user=UserHandle{0} id=1243972300 tag=null importance=3 key=0|com.max.xiaoheihe|1243972300|null|10087: Notification(channel=Default))
      uid=10087 userId=0
      notification=
            when=1793108245419
            extras={
                android.title=String (黑盒今日热点订阅>>)
                android.text=String (①仙剑四重制12月5日公布发售日
②另一条)
            }
    NotificationRecord(0x0477b099: pkg=com.max.xiaoheihe user=UserHandle{0} id=1882303187 tag=null importance=3 key=0|com.max.xiaoheihe|1882303187|null|10087: Notification(channel=Default))
      uid=10087 userId=0
      notification=
            when=1793063738911
            extras={
                android.title=String (芊星回复了你)
                android.text=String (来自“手多了一下”)
            }
    NotificationRecord(0x0f66c586: pkg=com.tencent.mobileqq user=UserHandle{0} id=999 tag=null importance=3 key=x)
      notification=
            when=1793000000000
            extras={
                android.title=String (某人提到了你)
                android.text=String (ignored package)
            }
'''


class ParseTests(unittest.TestCase):
    def test_reads_title_text_and_when(self):
        records = parse_notifications(DUMP)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]['title'], '黑盒今日热点订阅>>')
        self.assertIn('②另一条', records[0]['text'])
        self.assertEqual(records[1]['title'], '芊星回复了你')
        self.assertEqual(records[1]['text'], '来自“手多了一下”')
        self.assertEqual(records[1]['when'], 1793063738911)

    def test_classifies_reply_and_mention(self):
        self.assertEqual(classify('芊星回复了你'), ('reply', '芊星'))
        self.assertEqual(classify('某人提到了你'), ('mention', '某人'))
        self.assertEqual(classify('Steam'), ('other', None))

    def test_keeps_only_this_app_and_only_replies(self):
        events = to_events(parse_notifications(DUMP))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['kind'], 'reply')
        self.assertEqual(events[0]['author'], '芊星')
        self.assertEqual(events[0]['post_title'], '来自“手多了一下”')

    def test_fingerprint_is_stable_and_distinct(self):
        events = to_events(parse_notifications(DUMP))
        again = to_events(parse_notifications(DUMP))
        self.assertEqual(events[0]['event_id'], again[0]['event_id'])
        record = {'title': '芊星回复了你', 'text': '来自“手多了一下”', 'when': 1}
        self.assertNotEqual(fingerprint(record), fingerprint({**record, 'when': 2}))

    def test_garbage_input_is_empty(self):
        self.assertEqual(parse_notifications(None), [])
        self.assertEqual(parse_notifications(''), [])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = EventLedger(Path(self.temp.name) / 'ledger.db')
        self.addCleanup(self.ledger.close)

    def test_records_once_and_counts_repeats(self):
        events = to_events(parse_notifications(DUMP))
        self.assertEqual(self.ledger.record(events, 1000), 1)
        self.assertEqual(self.ledger.record(events, 2000), 0)
        pending = self.ledger.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['seen_count'], 2)
        self.assertEqual(self.ledger.counts(), {'total': 1, 'pending': 1})

    def test_actor_totals_are_kept(self):
        events = to_events(parse_notifications(DUMP))
        self.ledger.record(events, 1000)
        self.assertEqual(self.ledger.actor('芊星')['event_count'], 1)

    def test_mark_changes_status(self):
        events = to_events(parse_notifications(DUMP))
        self.ledger.record(events, 1000)
        event_id = self.ledger.pending()[0]['event_id']
        self.ledger.mark([event_id], 'seen')
        self.assertEqual(self.ledger.pending(), [])
        self.assertEqual(self.ledger.counts()['pending'], 0)


if __name__ == '__main__':
    unittest.main()
