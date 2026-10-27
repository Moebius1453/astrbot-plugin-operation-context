import unittest
from unittest.mock import AsyncMock

from ..app.phone_publication import PhonePostDriver
from ..app.publication import Publication


class PostDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_checks_receipt(self):
        execute = AsyncMock(return_value={'exit_code': 0, 'stdout': '{"submit_tapped":true}'})
        driver = PhonePostDriver(execute, lambda record: None)
        text = '【测试】测试 2026-10-27 16:23:55'
        self.assertEqual(await driver.submit(Publication('post', 'own_account', text, text)), {'submit_tapped': True})
        self.assertEqual(execute.await_count, 1)

    async def test_submit_failure_never_retries(self):
        execute = AsyncMock(return_value={'exit_code': 1})
        driver = PhonePostDriver(execute, lambda record: None)
        text = '【测试】测试 2026-10-27 16:23:55'
        with self.assertRaises(ValueError):
            await driver.submit(Publication('post', 'own_account', text, text))
        self.assertEqual(execute.await_count, 1)
