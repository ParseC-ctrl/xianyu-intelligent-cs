import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from main import XianyuLive
from context_manager import ChatContextManager
from delivery_store import DeliveryStore


class CustomerMessageBatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.agent = XianyuLive.__new__(XianyuLive)
        self.agent.knowledge_only = True
        self.agent.myid = 'seller'
        self.agent.message_expire_time = 300000
        self.agent.delivery_store = DeliveryStore(str(Path(self.temp.name) / 'delivery.db'))
        self.agent.context_manager = ChatContextManager(db_path=str(Path(self.temp.name) / 'chat.db'))

    def enqueue(self, key='next', buyer='buyer', item='123', text='皮鞋', chat='chat'):
        payload = {'1': {'2': chat + '@goofish', '5': int(time.time() * 1000),
                         '10': {'senderUserId': buyer, 'itemId': item, 'reminderContent': text}}}
        self.agent.delivery_store.record_inbound_event(key, 'chat:' + chat, payload)

    async def test_queue_is_filtered_by_buyer_item_and_chat(self):
        self.enqueue('foreign-buyer', buyer='someone')
        self.enqueue('foreign-item', item='456')
        self.enqueue('foreign-chat', chat='elsewhere')
        self.enqueue('seller-echo', buyer='seller')
        self.assertFalse(self.agent._pending_buyer_fragments('chat', 'buyer', '123'))
        self.enqueue()
        self.assertTrue(self.agent._pending_buyer_fragments('chat', 'buyer', '123'))
        self.assertEqual(await self.agent._wait_for_buyer_pause('chat', 'buyer', '123'), (False, False))

    async def test_arrival_during_quiet_period_defers(self):
        async def arrival(_seconds):
            self.enqueue()
        with patch('main.asyncio.sleep', side_effect=arrival) as sleep:
            ready, forced = await self.agent._wait_for_buyer_pause('chat', 'buyer', '123')
        sleep.assert_awaited_once_with(3.0)
        self.assertFalse(ready)
        self.assertFalse(forced)

    async def test_quiet_period_and_maximum_wait(self):
        async def noop(_seconds):
            return
        with patch('main.asyncio.sleep', side_effect=noop):
            self.assertEqual(await self.agent._wait_for_buyer_pause('chat', 'buyer', '123'), (True, False))
        self.agent._buyer_batch_starts[('chat', 'buyer', '123')] = time.monotonic() - 13
        self.enqueue()
        self.assertEqual(await self.agent._wait_for_buyer_pause('chat', 'buyer', '123'), (True, True))

    async def test_original_fragments_persist_and_merge_in_order(self):
        context = self.agent.context_manager
        for source, item, role, text in [('old', '123', 'assistant', '上一轮答复'),
                                         ('a', '123', 'user', '我40码'),
                                         ('other', '456', 'user', '别的商品'),
                                         ('b', '123', 'user', '平时穿皮鞋'),
                                         ('c', '123', 'user', '这双41码能穿吗')]:
            context.add_message_by_chat('chat', 'buyer', item, role, text, source_id=source)
        combined, count = self.agent._buyer_batch_content('chat', '123')
        self.assertEqual(combined, '我40码\n平时穿皮鞋\n这双41码能穿吗')
        self.assertEqual(count, 3)
        self.assertEqual(context.get_source_message('a')['content'], '我40码')


if __name__ == '__main__':
    unittest.main()
