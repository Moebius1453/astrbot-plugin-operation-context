import tempfile
import unittest
from pathlib import Path

from ..app.task_store import TaskStore


class TaskStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'task_state'
        self.store = TaskStore(self.root)

    async def test_create_update_and_read_back(self):
        record = await self.store.create(
            umo='umo', conversation_id='conversation-1',
            kind='phone_agent', description='看看手机',
        )
        self.assertEqual(record['status'], 'queued')
        self.assertNotIn('delivery', record)
        updated = await self.store.update(record['task_id'], status='running')
        self.assertEqual(updated['status'], 'running')
        self.assertEqual((await self.store.get(record['task_id']))['status'], 'running')

    async def test_restart_marks_unfinished_work_interrupted(self):
        running = await self.store.create(
            umo='umo', conversation_id='c', kind='phone_agent', description='g',
        )
        await self.store.update(running['task_id'], status='running')
        done = await self.store.create(
            umo='umo', conversation_id='c', kind='sandbox_worker', description='g',
        )
        await self.store.update(done['task_id'], status='completed')
        interrupted = await self.store.interrupt_running()
        self.assertEqual(interrupted, [running['task_id']])
        record = await self.store.get(running['task_id'])
        self.assertEqual(record['status'], 'interrupted')
        self.assertIn('not replayed', record['summary'])
        untouched = await self.store.get(done['task_id'])
        self.assertEqual(untouched['status'], 'completed')

    async def test_tasks_are_listed_per_conversation(self):
        await self.store.create(
            umo='a', conversation_id='c', kind='phone_agent', description='g',
        )
        await self.store.create(
            umo='b', conversation_id='c', kind='phone_agent', description='g',
        )
        self.assertEqual(len(await self.store.list_for('a')), 1)
        self.assertEqual(len(await self.store.list_for('b')), 1)
