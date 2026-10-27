import unittest

from ..app.notification_poll import NotificationPoller


class PollerTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_passes_the_dump_to_the_recorder(self):
        seen = []

        async def fetch():
            return 'dump text'

        def record(dump):
            seen.append(dump)
            return 2

        poller = NotificationPoller(fetch, record)
        self.assertEqual(await poller.run_once(), 2)
        self.assertEqual(seen, ['dump text'])

    async def test_missing_sandbox_skips_quietly(self):
        async def fetch():
            return None

        poller = NotificationPoller(fetch, lambda dump: 1)
        self.assertIsNone(await poller.run_once())

    async def test_interval_has_a_floor(self):
        poller = NotificationPoller(lambda: None, lambda dump: 0, interval=1)
        self.assertEqual(poller.interval, 60)


if __name__ == '__main__':
    unittest.main()
