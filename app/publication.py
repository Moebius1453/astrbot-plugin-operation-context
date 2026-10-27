import asyncio
import hashlib
import json
import re
from datetime import datetime
from dataclasses import asdict, dataclass
from pathlib import Path

from filelock import FileLock


@dataclass(frozen=True)
class Publication:
    action: str
    target: str
    text: str
    title: str = ''

    def validate(self):
        if self.action not in {'post', 'comment'}:
            raise ValueError('unsupported_publication')
        if not isinstance(self.text, str) or re.fullmatch(r'【测试】测试 \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', self.text) is None:
            raise ValueError('test_text_required')
        datetime.strptime(self.text.removeprefix('【测试】测试 '), '%Y-%m-%d %H:%M:%S')
        if self.action == 'post':
            if self.target != 'own_account' or self.title != self.text:
                raise ValueError('invalid_test_post')
        elif not self.target or self.title:
            raise ValueError('invalid_test_comment')

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


TEST_AUTHORIZATION_ID = 'user-20261027-test-post-and-comment'


class PublicationChain:
    @classmethod
    def authorized_test(cls, host_data: Path, post: Publication, comment_text: str):
        directory = host_data / 'publication_authorizations' / TEST_AUTHORIZATION_ID
        chain = cls(directory, post, comment_text)
        try:
            chain.initialize()
        except FileExistsError:
            chain._check_authorization()
        return chain

    def __init__(self, directory: Path, post: Publication, comment_text: str):
        post.validate()
        Publication('comment', 'pending_own_test_post', comment_text).validate()
        self.directory = directory
        self.post = post
        self.comment_text = comment_text
        self._lock = asyncio.Lock()

    def initialize(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        self._write_new('authorization.json', {
            'post': asdict(self.post), 'comment_text': self.comment_text,
            'maximum_attempts': 2,
        })

    def _write_new(self, filename, payload):
        with (self.directory / filename).open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            import os
            os.fsync(stream.fileno())

    def _check_authorization(self):
        recorded = json.loads((self.directory / 'authorization.json').read_text(encoding='utf-8'))
        if recorded != {'post': asdict(self.post), 'comment_text': self.comment_text, 'maximum_attempts': 2}:
            raise ValueError('authorization_changed')

    async def execute(self, payload: Publication, driver):
        payload.validate()
        async with self._lock:
            lock = FileLock(str(self.directory / 'chain.lock'), timeout=0)
            with lock:
                self._check_authorization()
                if payload.action == 'post':
                    if payload != self.post:
                        raise ValueError('payload_changed')
                else:
                    previous = json.loads((self.directory / 'post-result.json').read_text(encoding='utf-8'))
                    if previous.get('status') != 'read_back_confirmed' or not previous.get('object_id'):
                        raise ValueError('post_not_confirmed')
                    if payload != Publication('comment', previous['object_id'], self.comment_text):
                        raise ValueError('comment_target_or_text_changed')
                attempt = self.directory / (payload.action + '-attempt.json')
                if attempt.exists():
                    raise ValueError('attempt_already_consumed_no_retry')
                prepared = await driver.prepare(payload)
                if prepared != {'target': payload.target, 'text': payload.text, 'title': payload.title}:
                    raise ValueError('editor_or_target_mismatch')
                self._write_new(attempt.name, {
                    'status': 'unknown', 'fingerprint': payload.fingerprint(),
                    'payload': asdict(payload),
                })
                try:
                    receipt = await driver.submit(payload)
                    observed = await driver.read_back(payload, receipt)
                    confirmed = (
                        isinstance(observed, dict)
                        and observed.get('text') == payload.text
                        and observed.get('target') == payload.target
                        and observed.get('title', '') == payload.title
                        and isinstance(observed.get('object_id'), str)
                        and bool(observed['object_id'])
                    )
                    result = {
                        'status': 'read_back_confirmed' if confirmed else 'unknown',
                        'object_id': observed['object_id'] if confirmed else None,
                    }
                except asyncio.CancelledError:
                    self._write_new(payload.action + '-result.json', {'status': 'unknown', 'object_id': None})
                    raise
                except Exception as exc:
                    result = {'status': 'unknown', 'object_id': None, 'error_type': type(exc).__name__}
                self._write_new(payload.action + '-result.json', result)
                return result
