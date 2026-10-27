import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace

from ..app.publication import Publication, PublicationChain


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.text = '【测试】测试 2026-10-27 15:00:00'
        self.post = Publication('post', 'own_account', self.text, self.text)
        self.chain = PublicationChain(Path(self.temp.name) / 'chain', self.post, self.text)
        self.chain.initialize()

    def driver(self, payload, identifier='test-post'):
        return SimpleNamespace(
            prepare=AsyncMock(return_value={'target': payload.target, 'text': payload.text, 'title': payload.title}),
            submit=AsyncMock(return_value={'receipt': 'attempted'}),
            read_back=AsyncMock(return_value={'target': payload.target, 'text': payload.text, 'title': payload.title, 'object_id': identifier}),
        )

    async def test_post_then_comment_on_confirmed_test_post(self):
        result = await self.chain.execute(self.post, self.driver(self.post))
        self.assertEqual(result['status'], 'read_back_confirmed')
        comment = Publication('comment', 'test-post', self.text)
        self.assertEqual((await self.chain.execute(comment, self.driver(comment, 'test-comment')))['status'], 'read_back_confirmed')

    async def test_host_authorization_cannot_reset_consumed_post(self):
        root = Path(self.temp.name) / 'host'
        first = PublicationChain.authorized_test(root, self.post, self.text)
        await first.execute(self.post, self.driver(self.post))
        second = PublicationChain.authorized_test(root, self.post, self.text)
        driver = self.driver(self.post)
        with self.assertRaisesRegex(ValueError, 'attempt_already_consumed'):
            await second.execute(self.post, driver)
        driver.prepare.assert_not_called()

    def test_host_authorization_rejects_new_timestamp(self):
        root = Path(self.temp.name) / 'host'
        PublicationChain.authorized_test(root, self.post, self.text)
        other_text = '【测试】测试 2026-10-27 15:00:01'
        with self.assertRaisesRegex(ValueError, 'authorization_changed'):
            PublicationChain.authorized_test(root, Publication('post', 'own_account', other_text, other_text), other_text)

    def test_test_payload_requires_only_valid_timestamp(self):
        for text in ('【测试】测试 invented', self.text + 'extra', '【测试】测试 2026-99-27 15:00:00'):
            with self.assertRaises(ValueError):
                Publication('post', 'own_account', text, text).validate()

    async def test_unknown_never_retries(self):
        driver = self.driver(self.post)
        driver.submit.side_effect = TimeoutError()
        self.assertEqual((await self.chain.execute(self.post, driver))['status'], 'unknown')
        with self.assertRaises(ValueError):
            await self.chain.execute(self.post, driver)
        self.assertEqual(driver.submit.await_count, 1)

    async def test_editor_mismatch_does_not_submit(self):
        driver = self.driver(self.post)
        driver.prepare.return_value['text'] = 'invented'
        with self.assertRaises(ValueError):
            await self.chain.execute(self.post, driver)
        driver.submit.assert_not_called()

    async def test_payload_tampering_rejected(self):
        other_text = '【测试】测试 2026-10-27 15:00:01'
        other = Publication('post', 'own_account', other_text, other_text)
        driver = self.driver(other)
        with self.assertRaises(ValueError):
            await self.chain.execute(other, driver)
        driver.prepare.assert_not_called()

    async def test_comment_before_post_rejected(self):
        comment = Publication('comment', 'someone-else', self.text)
        driver = self.driver(comment)
        with self.assertRaises(FileNotFoundError):
            await self.chain.execute(comment, driver)
        driver.submit.assert_not_called()

    async def test_readback_mismatch_is_unknown(self):
        driver = self.driver(self.post)
        driver.read_back.return_value['text'] = 'different'
        self.assertEqual((await self.chain.execute(self.post, driver))['status'], 'unknown')

    async def test_duplicate_submission_rejected(self):
        driver = self.driver(self.post)
        await self.chain.execute(self.post, driver)
        with self.assertRaises(ValueError):
            await self.chain.execute(self.post, driver)
        self.assertEqual(driver.submit.await_count, 1)
