import asyncio
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import size_chart
from context_manager import ChatContextManager
from main import XianyuLive


class SizeChartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "size_chart.png").write_bytes(size_chart.PNG_SIGNATURE + b"fixture")
        self.db = str(self.root / "chat_history.db")
        self.manager = ChatContextManager(db_path=self.db)

    def tearDown(self):
        self.temp.cleanup()

    def agent(self):
        agent = object.__new__(XianyuLive)
        agent.state_dir = str(self.root)
        agent.chat_db_file = self.db
        agent.context_manager = self.manager
        agent.myid = "seller"
        agent.ws = object()
        agent.connection_ready = asyncio.Event()
        agent.connection_ready.set()
        return agent

    def test_intent_and_explicit_request(self):
        for text in ["脚长25厘米", "脚宽", "运动鞋穿42.5", "这双43码吗", "再发下尺码表"]:
            self.assertTrue(size_chart.wants_chart(text), text)
        self.assertFalse(size_chart.wants_chart("鞋面是什么材质"))
        self.assertTrue(size_chart.explicitly_requests_chart("再发下尺码表"))
        self.assertFalse(size_chart.explicitly_requests_chart("这个尺码多少"))
        self.assertTrue(size_chart.explicitly_requests_chart("再发一下那个尺码表"))
        self.assertFalse(size_chart.explicitly_requests_chart("有没有对应的尺码表"))
        history = [{"role": "assistant", "content": "尺码对照表前面发过啦，可以对照脚长和脚宽看看哈～"}]
        self.assertTrue(size_chart.explicitly_requests_chart("你再发一次", history))
        self.assertTrue(size_chart.explicitly_requests_chart("重新发一次尺码图"))
        self.assertTrue(size_chart.wants_chart("你再发一次", history))
        self.assertFalse(size_chart.explicitly_requests_chart("你再发一次", [{"role": "assistant", "content": "鞋子的图片在上面哦"}]))

    def test_delivery_status_distinguishes_confirmed_and_uncertain(self):
        digest = size_chart.read_chart(self.root)[1]
        self.assertEqual(size_chart.delivery_state(self.db, "chat", digest), "not_sent")
        self.assertTrue(size_chart.reserve(self.db, "chat", "one", digest))
        self.assertEqual(size_chart.delivery_state(self.db, "chat", digest), "uncertain")
        size_chart.finish(self.db, "chat", "one", "failed")
        self.assertEqual(size_chart.delivery_state(self.db, "chat", digest), "uncertain")
        self.assertTrue(size_chart.reserve(self.db, "chat", "two", digest, explicit=True))
        size_chart.finish(self.db, "chat", "two", "sent")
        self.assertEqual(size_chart.delivery_state(self.db, "chat", digest), "sent")

    def test_once_per_conversation_explicit_resend_and_failure(self):
        async def run():
            agent = self.agent()
            sends = []
            async def verify(): pass
            async def send(_ws, _chat, _recipient, _text, **kwargs):
                sends.append(kwargs["message_uuid"])
                self.assertTrue((self.root / kwargs["media"][0]["path"]).exists())
                return {"type": "image", "url": "https://cdn.example/fixture.png", "width": 100, "height": 100}
            agent.send_msg = send
            for source, message in [("one", "尺码多少"), ("two", "脚宽多少"), ("three", "再发下尺码表"), ("three", "再发下尺码表")]:
                await agent._send_size_chart_after_reply("chat", "buyer", "item", source, message, verify)
            history = [{"role": "assistant", "content": "尺码对照表前面发过啦"}]
            await agent._send_size_chart_after_reply("chat", "buyer", "item", "four", "你再发一次", verify, history=history)
            await agent._send_size_chart_after_reply("chat", "buyer", "item", "five", "重新发一次尺码图", verify)
            self.assertEqual(len(sends), 4)
            self.assertEqual(len(set(sends)), 4)
            self.assertEqual(len(list(self.root.glob("manual_reply_*.png"))), 0)
            async def fail(*args, **kwargs):
                sends.append("failure")
                raise TimeoutError()
            agent.send_msg = fail
            await agent._send_size_chart_after_reply("other", "buyer", "item", "four", "尺码多少", verify)
            await agent._send_size_chart_after_reply("other", "buyer", "item", "five", "尺码多少", verify)
            self.assertEqual(sends.count("failure"), 1)
            with sqlite3.connect(self.db) as conn:
                rows = conn.execute("SELECT status FROM size_chart_attempts WHERE chat_id='other'").fetchall()
            self.assertEqual(rows, [("failed",)])
        asyncio.run(run())

    def test_unavailable_and_pre_send_cancellation_do_not_send(self):
        async def run():
            agent = self.agent()
            calls = []
            async def verify(): raise RuntimeError("cancelled")
            async def send(*args, **kwargs): calls.append(True)
            agent.send_msg = send
            await agent._send_size_chart_after_reply("chat", "buyer", "item", "one", "尺码多少", verify)
            (self.root / "size_chart.png").unlink()
            await agent._send_size_chart_after_reply("chat", "buyer", "item", "two", "尺码多少", verify)
            self.assertEqual(calls, [])
        asyncio.run(run())

    def test_image_upload_and_websocket_payload_with_mock_platform(self):
        async def run():
            agent = self.agent()
            agent.pending_send_acks = {}
            agent.send_ack_timeout = 1
            uploaded, frames = [], []
            class FakeAPI:
                def upload_media(self, path):
                    uploaded.append(Path(path).read_bytes())
                    return {"object": {"url": "https://cdn.example/size.png", "pix": "640x480"}}
            class FakeSocket:
                async def send(self, raw):
                    frame = json.loads(raw)
                    frames.append(frame)
                    agent.pending_send_acks[frame["headers"]["mid"]].set_result(None)
            async def verify(): pass
            agent.xianyu = FakeAPI()
            agent.ws = FakeSocket()
            await agent._send_size_chart_after_reply("chat", "buyer", "item", "first", "尺码多少", verify)
            self.assertEqual(uploaded, [size_chart.PNG_SIGNATURE + b"fixture"])
            content = json.loads(base64.b64decode(frames[0]["body"][0]["content"]["custom"]["data"]))
            self.assertEqual(content["contentType"], 2)
            self.assertEqual(content["image"]["pics"][0]["url"], "https://cdn.example/size.png")
            with sqlite3.connect(self.db) as conn:
                self.assertEqual(conn.execute("SELECT status FROM size_chart_attempts").fetchone()[0], "sent")
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
