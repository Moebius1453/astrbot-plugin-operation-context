"""Event ledger for phone notifications.

Only facts go in here: who, which post, when, and which notification it came
from. The comment body itself is never in a notification, so it cannot go in
either. Readers decide what the facts mean; the ledger never decides anything.
"""

import hashlib
import re
import sqlite3
from pathlib import Path

PACKAGE = 'com.max.xiaoheihe'

_TITLE_PREFIX = 'android.title=String ('
_TEXT_PREFIX = 'android.text=String ('
_RECORD = re.compile(r'NotificationRecord\([^)]*?pkg=([\w.]+)[^)]*?id=(\d+)')
_WHEN = re.compile(r'^\s*when=(\d+)\s*$')
_REPLY = re.compile(r'^(.+?)回复了你$')
_MENTION = re.compile(r'^(.+?)提到了你$')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    package TEXT NOT NULL,
    kind TEXT NOT NULL,
    author TEXT,
    post_title TEXT,
    notified_at INTEGER,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'new',
    wake_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_seen ON events(first_seen DESC);
CREATE INDEX IF NOT EXISTS events_status ON events(status);
CREATE TABLE IF NOT EXISTS actors (
    name TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    handled_count INTEGER NOT NULL DEFAULT 0
);
'''


def _read_string(lines, start, prefix):
    value = lines[start].split(prefix, 1)[1]
    index = start
    while not value.rstrip().endswith(')') and index + 1 < len(lines):
        index += 1
        value += '\n' + lines[index]
    value = value.rstrip()
    return value[:-1] if value.endswith(')') else value


def parse_notifications(dump):
    """Turn one `dumpsys notification --noredact` dump into plain records."""
    if not isinstance(dump, str):
        return []
    blocks = []
    current = None
    for line in dump.splitlines():
        if 'NotificationRecord(' in line and 'pkg=' in line:
            current = [line]
            blocks.append(current)
        elif current is not None:
            current.append(line)
    records = []
    for block in blocks:
        head = _RECORD.search(block[0])
        if not head:
            continue
        package, notification_id = head.group(1), head.group(2)
        title = text = None
        when = None
        for index, line in enumerate(block):
            if title is None and _TITLE_PREFIX in line:
                title = _read_string(block, index, _TITLE_PREFIX)
            elif text is None and _TEXT_PREFIX in line:
                text = _read_string(block, index, _TEXT_PREFIX)
            elif when is None and _WHEN.match(line):
                when = int(_WHEN.match(line).group(1))
        records.append({
            'package': package,
            'notification_id': notification_id,
            'title': title,
            'text': text,
            'when': when,
        })
    return records


def classify(title):
    if not isinstance(title, str):
        return 'other', None
    for pattern, kind in ((_REPLY, 'reply'), (_MENTION, 'mention')):
        matched = pattern.match(title.strip())
        if matched:
            return kind, matched.group(1).strip()
    return 'other', None


def fingerprint(record):
    material = '|'.join(str(record.get(key)) for key in ('title', 'text', 'when'))
    return hashlib.sha1(material.encode('utf-8')).hexdigest()


def to_events(records, package=PACKAGE):
    """Keep only events worth remembering, as ledger rows."""
    events = []
    for record in records:
        if record.get('package') != package:
            continue
        kind, author = classify(record.get('title'))
        if kind == 'other':
            continue
        events.append({
            'event_id': fingerprint(record),
            'package': package,
            'kind': kind,
            'author': author,
            'post_title': record.get('text'),
            'notified_at': record.get('when'),
        })
    return events


class EventLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def record(self, events, now):
        added = 0
        for event in events:
            cursor = self.connection.execute(
                'INSERT OR IGNORE INTO events (event_id, package, kind, author, post_title, '
                'notified_at, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (event['event_id'], event['package'], event['kind'], event['author'],
                 event['post_title'], event['notified_at'], now, now),
            )
            if cursor.rowcount == 1:
                added += 1
                self._touch_actor(event['author'], now)
            else:
                self.connection.execute(
                    'UPDATE events SET last_seen = ?, seen_count = seen_count + 1 WHERE event_id = ?',
                    (now, event['event_id']),
                )
        self.connection.commit()
        return added

    def _touch_actor(self, name, now):
        if not name:
            return
        self.connection.execute(
            'INSERT INTO actors (name, first_seen, last_seen, event_count) VALUES (?, ?, ?, 1) '
            'ON CONFLICT(name) DO UPDATE SET last_seen = excluded.last_seen, '
            'event_count = event_count + 1',
            (name, now, now),
        )

    def pending(self, limit=20):
        rows = self.connection.execute(
            'SELECT event_id, kind, author, post_title, notified_at, first_seen, seen_count '
            'FROM events WHERE status = ? ORDER BY first_seen DESC LIMIT ?',
            ('new', limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark(self, event_ids, status):
        if not event_ids:
            return
        placeholders = ','.join('?' for _ in event_ids)
        self.connection.execute(
            f'UPDATE events SET status = ? WHERE event_id IN ({placeholders})',
            (status, *event_ids),
        )
        self.connection.commit()

    def actor(self, name):
        row = self.connection.execute('SELECT * FROM actors WHERE name = ?', (name,)).fetchone()
        return dict(row) if row else None

    def counts(self):
        row = self.connection.execute(
            'SELECT COUNT(*) AS total, '
            "SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END) AS pending "
            'FROM events'
        ).fetchone()
        return {'total': row['total'] or 0, 'pending': row['pending'] or 0}
