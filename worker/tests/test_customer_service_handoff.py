"""Offline contracts for knowledge-only customer service; synthetic data only."""
import asyncio
import hashlib
import time
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from delivery_store import DeliveryStore
from main import ManualTakeoverError, XianyuLive
from XianyuAgent import LLMConfigurationError, LLMServiceError, LLMTimeoutError, LLMNotReadyError, LLMResponseFormatError
from tests import test_free_automation as fixtures
from tests import test_manual_outbox as manual_fixtures
from tests import test_agent_resilience as client_fixtures


class PersistentTakeoverStoreTests(unittest.TestCase):
    def test_persistent_mode_survives_restart_and_clock_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "delivery.db")
            store = DeliveryStore(path, now_fn=lambda: 1000)
            store.set_manual_mode("chat", True, None)
            restarted = DeliveryStore(path, now_fn=lambda: 1e10)
            self.assertTrue(restarted.is_manual_mode("chat"))
            self.assertIsNone(restarted.manual_exit_at("chat"))
            restarted.set_manual_mode("chat", False, None)
            self.assertFalse(restarted.is_manual_mode("chat"))

    def test_claim_once_resume_reclaims_and_human_intervention_invalidates_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "delivery.db")
            store = DeliveryStore(path)
            token = store.claim_handoff("chat")
            self.assertTrue(store.handoff_notice_current("chat", token))
            self.assertIsNone(DeliveryStore(path).claim_handoff("chat"))
            store.set_manual_mode("chat", True, None)
            self.assertFalse(store.handoff_notice_current("chat", token))
            store.set_manual_mode("chat", False, None)
            next_token = store.claim_handoff("chat")
            self.assertNotEqual(token, next_token)
            self.assertTrue(store.handoff_notice_current("chat", next_token))

    def test_legacy_expiry_and_schema_migration_remain_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "delivery.db")
            with sqlite3.connect(path) as con:
                con.execute("CREATE TABLE manual_modes(chat_id TEXT PRIMARY KEY, expires_at REAL NOT NULL, updated_at REAL NOT NULL)")
            clock = [1000.0]
            store = DeliveryStore(path, now_fn=lambda: clock[0])
            store.set_manual_mode("temporary", True, 60)
            store.set_manual_mode("persistent", True, None)
            clock[0] += 61
            self.assertFalse(store.is_manual_mode("temporary"))
            self.assertEqual(store.manual_exit_at("temporary"), 1060)
            self.assertTrue(store.is_manual_mode("persistent"))
            self.assertEqual(store.toggle_manual_mode_once("resume", "persistent", None), ("auto", True))


class KnowledgeHandoffTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.FreeAutomationTests.setUp
    tearDown = fixtures.FreeAutomationTests.tearDown
    make_agent = fixtures.FreeAutomationTests.make_agent
    build_agent = fixtures.FreeAutomationTests.build_agent

    def enable_knowledge_mode(self):
        env = patch.dict(os.environ, {"SAAS_KNOWLEDGE_ONLY": "1", "SAAS_CUSTOMER_SERVICE_ONLY": "1"})
        env.start()
        self.addAsyncCleanup(env.stop)

    async def test_ai_handoff_persists_sends_once_and_explicit_resume_rearms(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=fixtures.FakeBot(decision="handoff", reason_code="out_of_scope"))
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "天气怎么样", "message-1")
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        self.assertTrue(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))
        self.assertEqual(agent.send_text_reliably.await_count, 1)
        self.assertEqual(agent.send_text_reliably.await_args.args[2], "我看看哈")
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "再问一次", "message-2")
        self.assertEqual(bot.calls, 1)
        self.assertEqual(agent.send_text_reliably.await_count, 1)
        agent.delivery_store = DeliveryStore(agent.delivery_db_file, now_fn=lambda: 1e10)
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        agent.exit_manual_mode(fixtures.CHAT_ID)
        self.assertFalse(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))
        agent.delivery_store = DeliveryStore(agent.delivery_db_file)
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "又一个无关问题", "message-3")
        self.assertEqual(agent.send_text_reliably.await_count, 2)
        self.assertTrue(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))

    async def test_missing_internal_ai_client_handoffs_once_and_stays_manual(self):
        self.enable_knowledge_mode()
        agent, _ = self.build_agent(mode="rules_ai")
        agent.bot = None
        for index in range(2):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", f"no-client:{index}")
        self.assertEqual(agent.send_text_reliably.await_count, 1)
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        self.assertTrue(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))

    async def test_configuration_error_handoffs_once_without_retrying_model(self):
        self.enable_knowledge_mode()
        agent, _ = self.build_agent(mode="rules_ai")
        agent._generate_llm_reply = AsyncMock(side_effect=LLMConfigurationError("synthetic configuration 409"))
        for index in range(2):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", f"bad-config:{index}")
        self.assertEqual(agent._generate_llm_reply.await_count, 1)
        self.assertEqual(agent.send_text_reliably.await_count, 1)
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        self.assertTrue(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))

    async def test_not_ready_cancels_without_platform_handoff_notice(self):
        self.enable_knowledge_mode()
        agent, _ = self.build_agent(mode="rules_ai")
        agent._generate_llm_reply = AsyncMock(side_effect=LLMNotReadyError("synthetic platform-sync protection"))
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", "sync-protection")
        agent.send_text_reliably.assert_not_awaited()
        self.assertFalse(agent.is_manual_mode(fixtures.CHAT_ID))

    async def test_platform_auth_failure_blocks_handoff_notice(self):
        self.enable_knowledge_mode()
        agent, _ = self.build_agent(mode="rules_ai")
        agent.authentication_failure_code = "verification_required"
        agent.token_circuit_open = True
        wire = AsyncMock()
        agent._customer_conversation_scope = AsyncMock(return_value="owned")
        async def transport(*args, **kwargs):
            await kwargs["before_attempt"]()
            await wire()
        agent.send_text_reliably = transport
        await agent._handoff_to_human(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "auth-failed")
        wire.assert_not_awaited()
        agent._customer_conversation_scope.assert_not_awaited()
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        self.assertTrue(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))

    async def test_budget_exhaustion_handoffs_once_without_model_call(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai")
        agent.delivery_store.reserve_llm_budget = Mock(return_value=False)
        for index in range(2):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", f"budget:{index}")
        self.assertEqual(bot.calls, 0)
        self.assertEqual(agent.send_text_reliably.await_count, 1)
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        self.assertTrue(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))

    async def test_timeout_and_service_retry_exhaustion_handoffs_once(self):
        self.enable_knowledge_mode()
        for case_index, error in enumerate((asyncio.TimeoutError(), LLMTimeoutError("synthetic timeout"), LLMServiceError("synthetic 503"))):
            with self.subTest(error=type(error).__name__):
                agent, _ = self.build_agent(mode="rules_ai")
                agent.exit_manual_mode(fixtures.CHAT_ID)
                agent.llm_retry_attempts = 2
                agent.llm_retry_delay = 0
                agent._generate_llm_reply = AsyncMock(side_effect=error)
                for index in range(2):
                    await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", f"retry:{case_index}:{index}")
                self.assertEqual(agent._generate_llm_reply.await_count, 2)
                self.assertEqual(agent.send_text_reliably.await_count, 1)
                self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
                self.assertTrue(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))

    async def test_knowledge_version_reaches_readiness_and_survives_draft_restart(self):
        self.enable_knowledge_mode()
        class VersionedBot(fixtures.FakeBot):
            def generate_reply_result(self, *args, **kwargs):
                return {**super().generate_reply_result(*args, **kwargs), "knowledge_version": "a" * 64}
            def ensure_ready(self, expected_config_revision, expected_knowledge_version=None):
                self.received_knowledge_version = expected_knowledge_version
                if expected_knowledge_version != "a" * 64:
                    raise LLMNotReadyError("synthetic version mismatch")
                return super().ensure_ready(expected_config_revision)
        agent, bot = self.build_agent(mode="rules_ai", bot=VersionedBot("鞋面材质是头层牛皮"))
        async def interruption(*args, **kwargs):
            await kwargs["before_attempt"]()
            raise ConnectionError("synthetic restart before send")
        agent.send_text_reliably = interruption
        with self.assertRaises(ConnectionError):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", "versioned-draft")
        self.assertEqual(bot.received_knowledge_version, "a" * 64)
        provenance = agent._assistant_draft_provenance("assistant:versioned-draft")
        self.assertEqual(provenance["knowledge_version"], "a" * 64)
        restarted, next_bot = self.build_agent(mode="rules_ai", bot=VersionedBot("不得重新生成"))
        sent = AsyncMock()
        async def send(*args, **kwargs):
            await kwargs["before_attempt"]()
            await sent(*args)
        restarted.send_text_reliably = send
        await restarted._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", "versioned-draft")
        self.assertEqual(next_bot.calls, 0)
        self.assertEqual(next_bot.received_knowledge_version, "a" * 64)
        sent.assert_awaited_once()

    async def test_readiness_service_failure_handoffs_without_sending_material_answer(self):
        self.enable_knowledge_mode()
        class VersionedBot(fixtures.FakeBot):
            def generate_reply_result(self, *args, **kwargs):
                return {**super().generate_reply_result(*args, **kwargs), "knowledge_version": "a" * 64}
            def ensure_ready(self, expected_config_revision, expected_knowledge_version=None):
                raise LLMServiceError("synthetic readiness 503")
        agent, _ = self.build_agent(mode="rules_ai", bot=VersionedBot("不应发送的材质答案"))
        sent = AsyncMock()
        async def send(*args, **kwargs):
            await kwargs["before_attempt"]()
            await sent(*args)
        agent.send_text_reliably = send
        for index in range(2):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", f"ready-failed:{index}")
        sent.assert_awaited_once()
        self.assertEqual(sent.await_args.args[2], "我看看哈")
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        self.assertTrue(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))

    async def test_changed_knowledge_version_cancels_unsent_reply(self):
        self.enable_knowledge_mode()
        class VersionedBot(fixtures.FakeBot):
            def generate_reply_result(self, *args, **kwargs):
                return {**super().generate_reply_result(*args, **kwargs), "knowledge_version": "a" * 64}
            def ensure_ready(self, expected_config_revision, expected_knowledge_version=None):
                self.received_knowledge_version = expected_knowledge_version
                raise LLMNotReadyError("synthetic Excel or binding changed")
        agent, bot = self.build_agent(mode="rules_ai", bot=VersionedBot("鞋面材质是头层牛皮"))
        sent = AsyncMock()
        async def send(*args, **kwargs):
            await kwargs["before_attempt"]()
            await sent(*args)
        agent.send_text_reliably = send
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", "changed-version")
        self.assertEqual(bot.received_knowledge_version, "a" * 64)
        sent.assert_not_awaited()
        self.assertEqual(agent.context_manager.get_source_message("assistant:changed-version")["role"], "assistant_cancelled")

    async def test_same_material_question_different_sources_replies_twice(self):
        self.enable_knowledge_mode()
        class MaterialBot(fixtures.FakeBot):
            def generate_reply_result(self, *args, **kwargs):
                return {**super().generate_reply_result(*args, **kwargs), "knowledge_version": "a" * 64}
        agent, bot = self.build_agent(mode="rules_ai", bot=MaterialBot("这款鞋的鞋面材质是头层牛皮。"))
        sources = ("material-repeat-1", "material-repeat-2")
        for source in sources:
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", source)
        self.assertEqual(bot.calls, 2, "Each question needs classification once, without a duplicate retry")
        self.assertEqual(agent.send_text_reliably.await_count, 2)
        self.assertEqual(agent.send_text_reliably.await_args_list[0].args[2], agent.send_text_reliably.await_args_list[1].args[2])
        for source in sources:
            self.assertEqual(agent.context_manager.get_source_message("assistant:" + source)["role"], "assistant")
        self.assertFalse(agent.is_manual_mode(fixtures.CHAT_ID))

    async def test_same_material_source_replay_sends_only_once(self):
        self.enable_knowledge_mode()
        class MaterialBot(fixtures.FakeBot):
            def generate_reply_result(self, *args, **kwargs):
                return {**super().generate_reply_result(*args, **kwargs), "knowledge_version": "a" * 64}
        agent, bot = self.build_agent(mode="rules_ai", bot=MaterialBot("这款鞋的鞋面材质是头层牛皮。"))
        for _ in range(2):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面是什么材质", "material-source-replay")
        self.assertEqual(bot.calls, 1)
        agent.send_text_reliably.assert_awaited_once()
        self.assertEqual(agent.context_manager.get_source_message("assistant:material-source-replay")["role"], "assistant")
        self.assertFalse(agent.is_manual_mode(fixtures.CHAT_ID))

    async def test_human_intent_uses_model_before_keyword_rules(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=fixtures.FakeBot(decision="handoff", reason_code="buyer_requests_human"), rules={"version": 1, "rules": [{"id": "unsafe", "keywords": ["人工"], "reply": "规则不能抢答"}]})
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "我要转人工", "manual-request")
        self.assertEqual(bot.calls, 1)
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        self.assertEqual(agent.send_text_reliably.await_args.args[2], "我看看哈")

    def local_scope(self, agent, *, with_snapshot=True):
        ref = hashlib.sha256(str(agent.myid).encode()).hexdigest()[:16]
        agent.context_manager.set_customer_conversation_scope(fixtures.CHAT_ID, fixtures.API_ITEM, ref, "owned", checked_at=time.time())
        if with_snapshot:
            fixtures.write_json(self.state / "shop_snapshot.json", {"version": 1, "account_ref": ref,
                                  "products": [{"id": fixtures.API_ITEM, "title": "测试鞋 TEST100-01", "codes": ["TEST100-01"]}]})
        agent.xianyu.get_item_info = Mock(side_effect=AssertionError("Knowledge-only must never fetch item details"))
        agent.xianyu.get_message_head_info = Mock(side_effect=AssertionError("Fresh locally verified scope needs no head request"))
        return ref

    def versioned_bot(self, reply, *, reason_code="material_answered", decision="reply"):
        class LocalBot(fixtures.FakeBot):
            def generate_reply_result(self, *args, **kwargs):
                return {**super().generate_reply_result(*args, **kwargs), "knowledge_version": "a" * 64}
        return LocalBot(reply, decision=decision, reason_code=reason_code)

    async def test_greeting_material_faq_and_refusal_need_no_platform_details(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=self.versioned_bot("您好，可以咨询商品材质。", reason_code="greeting_answered"))
        self.local_scope(agent)
        cases = (("你好", "您好，可以咨询商品材质。", "greeting_answered"),
                 ("鞋面材质", "鞋面材质是头层牛皮。", "material_answered"),
                 ("发货说明", "按已审核说明发货。", "faq_answered"),
                 ("讲个故事", "可以咨询商品材质或已审核的问题。", "out_of_scope"))
        for index, (question, reply, reason) in enumerate(cases):
            bot.reply, bot.last_reason_code = reply, reason
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, question, f"no-platform:{index}")
        self.assertEqual(bot.calls, len(cases))
        self.assertEqual(agent.send_text_reliably.await_count, len(cases))
        agent.xianyu.get_item_info.assert_not_called()
        agent.xianyu.get_message_head_info.assert_not_called()
        self.assertEqual(bot.requests[0]["item_context"]["codes"], ["TEST100-01"])

    async def test_missing_or_expired_local_item_cache_does_not_refresh_online(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=self.versioned_bot("您好，可以咨询商品材质。", reason_code="greeting_answered"))
        self.local_scope(agent, with_snapshot=False)
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "你好", "local-missing")
        self.assertEqual(bot.requests[0]["item_context"], {"item_id": fixtures.API_ITEM})
        agent.context_manager.save_item_info(fixtures.API_ITEM, {"title": "过期的商品资料"})
        with sqlite3.connect(agent.chat_db_file) as conn:
            conn.execute("UPDATE items SET last_updated = ? WHERE item_id = ?", ("2000-01-01T00:00:00", fixtures.API_ITEM))
        bot.reply, bot.last_reason_code = "鞋面材质是头层牛皮。", "material_answered"
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面材质", "local-expired")
        self.assertEqual(bot.requests[1]["item_context"], {"item_id": fixtures.API_ITEM})
        agent.xianyu.get_item_info.assert_not_called()
        agent.xianyu.get_message_head_info.assert_not_called()

    async def test_human_negation_and_product_greeting_follow_semantic_reply(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=self.versioned_bot("您好，可以咨询商品材质。", reason_code="greeting_answered"))
        self.local_scope(agent)
        for index, text in enumerate(("你好，找商品", "不要联系人工客服，鞋面是什么材质")):
            if index:
                bot.reply, bot.last_reason_code = "鞋面材质是头层牛皮。", "material_answered"
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, text, f"semantic:{index}")
        self.assertEqual(bot.calls, 2)
        self.assertEqual(agent.send_text_reliably.await_count, 2)
        self.assertFalse(agent.is_manual_mode(fixtures.CHAT_ID))
        agent.xianyu.get_item_info.assert_not_called()
        agent.xianyu.get_message_head_info.assert_not_called()

    async def test_new_greeting_messages_are_answered_without_model_retry(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=self.versioned_bot("您好，可以咨询商品材质。", reason_code="greeting_answered"))
        self.local_scope(agent)
        for index in range(2):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "你好", f"greet:{index}")
        self.assertEqual(bot.calls, 2)
        self.assertEqual(agent.send_text_reliably.await_count, 2)
        self.assertEqual(agent.context_manager.get_source_message("assistant:greet:1")["role"], "assistant")
        self.assertFalse(agent.is_manual_mode(fixtures.CHAT_ID))

    async def test_new_refusal_and_faq_messages_are_answered(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=self.versioned_bot("可以咨询商品材质或已审核的问题。", reason_code="out_of_scope"))
        self.local_scope(agent)
        for index in range(2):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "讲个故事", f"refuse:{index}")
        self.assertEqual(agent.send_text_reliably.await_count, 2)
        bot.reply, bot.last_reason_code = "按已审核说明发货。", "faq_answered"
        for index in range(2):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "发货说明", f"faq:{index}")
        self.assertEqual(bot.calls, 4)
        self.assertEqual(agent.send_text_reliably.await_count, 4)
        self.assertFalse(agent.is_manual_mode(fixtures.CHAT_ID))

    async def test_new_topic_then_greeting_and_distinct_refusals_are_answered(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=self.versioned_bot("您好，可以咨询商品材质。", reason_code="greeting_answered"))
        self.local_scope(agent)
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "你好", "topic:greet1")
        bot.reply, bot.last_reason_code = "按页面标价出售。", "faq_answered"
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "便宜点", "topic:price")
        bot.reply, bot.last_reason_code = "您好，可以咨询商品材质。", "greeting_answered"
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "你好", "topic:greet2")
        self.assertEqual(agent.send_text_reliably.await_count, 3)
        bot.reply, bot.last_reason_code = "请咨询商品相关问题。", "out_of_scope"
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "天气", "topic:weather")
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "足球", "topic:sports")
        self.assertEqual(agent.send_text_reliably.await_count, 5)
        self.assertFalse(agent.is_manual_mode(fixtures.CHAT_ID))

    async def test_foreign_scope_cannot_be_overridden_by_owned_snapshot(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=self.versioned_bot("您好", reason_code="greeting_answered"))
        ref = self.local_scope(agent)
        agent.context_manager.set_customer_conversation_scope(fixtures.CHAT_ID, fixtures.API_ITEM, ref, "foreign", checked_at=time.time())
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "你好", "foreign-owned-product")
        self.assertEqual(bot.calls, 0)
        agent.send_text_reliably.assert_not_awaited()
        agent.xianyu.get_message_head_info.assert_not_called()
        agent.xianyu.get_item_info.assert_not_called()

    async def test_expired_owned_scope_is_not_extended_without_confirmation(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=self.versioned_bot("您好", reason_code="greeting_answered"))
        ref = self.local_scope(agent, with_snapshot=False)
        agent.context_manager.set_customer_conversation_scope(fixtures.CHAT_ID, fixtures.API_ITEM, ref, "owned", checked_at=time.time() - 301)
        agent.xianyu.get_message_head_info = Mock(return_value={"data": {"commonData": {}}})
        for index in range(2):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "你好", f"unknown:{index}")
        agent.xianyu.get_message_head_info.assert_called_once()
        agent.xianyu.get_item_info.assert_not_called()
        self.assertEqual(bot.calls, 0)
        agent.send_text_reliably.assert_not_awaited()
        current = agent.context_manager.get_customer_conversation_scope(fixtures.CHAT_ID, fixtures.API_ITEM, ref)
        self.assertEqual(current["scope"], "unknown")

    async def test_expired_foreign_scope_requires_head_instead_of_snapshot_override(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=self.versioned_bot("您好", reason_code="greeting_answered"))
        ref = self.local_scope(agent)
        agent.context_manager.set_customer_conversation_scope(fixtures.CHAT_ID, fixtures.API_ITEM, ref, "foreign", checked_at=time.time() - 301)
        agent.xianyu.get_message_head_info = Mock(return_value={"data": {"commonData": {"seller": False, "itemId": fixtures.API_ITEM}}})
        for index in range(2):
            await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "你好", f"foreign-expired:{index}")
        agent.xianyu.get_message_head_info.assert_called_once()
        agent.xianyu.get_item_info.assert_not_called()
        self.assertEqual(bot.calls, 0)
        agent.send_text_reliably.assert_not_awaited()

    async def test_auth_failure_blocks_scope_requests_classification_and_send(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=self.versioned_bot("您好", reason_code="greeting_answered"))
        self.local_scope(agent)
        agent.authentication_failure_code = "verification_required"
        agent.token_circuit_open = True
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "你好", "auth-scope-stop")
        self.assertEqual(bot.calls, 0)
        agent.send_text_reliably.assert_not_awaited()
        agent.xianyu.get_item_info.assert_not_called()
        agent.xianyu.get_message_head_info.assert_not_called()

    async def test_keyword_rule_does_not_bypass_knowledge_intent_gate(self):
        self.enable_knowledge_mode()
        agent, bot = self.build_agent(mode="rules_ai", bot=fixtures.FakeBot(decision="handoff"), rules={"version": 1, "rules": [{"id": "broad", "keywords": ["你好"], "reply": "规则回复"}]})
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "你好，退款吗", "gate-1")
        self.assertEqual(bot.calls, 1)
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        self.assertNotEqual(agent.send_text_reliably.await_args.args[2], "规则回复")

    async def test_takeover_during_generation_cancels_unsent_ai_reply(self):
        self.enable_knowledge_mode()
        agent, _ = self.build_agent(mode="rules_ai")
        async def generating(*_args):
            agent.enter_manual_mode(fixtures.CHAT_ID)
            return "鞋面为头层牛皮", "reply", "knowledge_match", 0
        agent._generate_llm_reply = generating
        await agent._process_buyer_chat(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "鞋面材质", "race-1")
        agent.send_text_reliably.assert_not_awaited()
        self.assertEqual(agent.context_manager.get_source_message("assistant:race-1")["role"], "assistant_cancelled")

    async def test_buyer_notice_uses_store_owner_wording_and_sends_once(self):
        self.enable_knowledge_mode()
        agent, _ = self.build_agent(mode="rules_ai")
        await agent._handoff_to_human(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "friendly-notice")
        await agent._handoff_to_human(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "friendly-notice-again")
        self.assertEqual(agent.send_text_reliably.await_count, 1)
        text = agent.send_text_reliably.await_args.args[2]
        self.assertEqual(text, "我看看哈")
        self.assertNotIn("人工", text)
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))

    async def test_operator_cancels_queued_handoff_notice(self):
        self.enable_knowledge_mode()
        agent, _ = self.build_agent(mode="rules_ai")
        wire_send = AsyncMock()
        async def queued_notice(*_args, **kwargs):
            agent.enter_manual_mode(fixtures.CHAT_ID)
            await kwargs["before_attempt"]()
            await wire_send()
        agent.send_text_reliably = queued_notice
        await agent._handoff_to_human(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "queued-notice")
        wire_send.assert_not_awaited()
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        self.assertTrue(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))

    async def test_notice_failure_stays_pending_without_repeat(self):
        self.enable_knowledge_mode()
        agent, _ = self.build_agent(mode="rules_ai")
        agent.send_text_reliably.side_effect = ConnectionError("synthetic connection loss")
        await agent._handoff_to_human(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "notice-failed")
        await agent._handoff_to_human(fixtures.CHAT_ID, fixtures.BUYER_ID, fixtures.API_ITEM, "notice-failed-again")
        self.assertEqual(agent.send_text_reliably.await_count, 1)
        self.assertTrue(agent.is_manual_mode(fixtures.CHAT_ID))
        self.assertTrue(agent.context_manager.conversation_needs_human(fixtures.CHAT_ID))

    async def test_pre_send_callback_takeover_is_rechecked_after_await(self):
        self.enable_knowledge_mode()
        agent, _ = self.build_agent(mode="rules_ai")
        agent.send_text_reliably = XianyuLive.send_text_reliably.__get__(agent)
        agent.connection_ready.set()
        agent.ws = Mock()
        agent.send_msg = AsyncMock()
        async def direct(operation):
            return await operation()
        async def verify():
            await asyncio.sleep(0)
            agent.enter_manual_mode(fixtures.CHAT_ID)
        agent._send_with_pacing = direct
        with self.assertRaises(ManualTakeoverError):
            await agent.send_text_reliably(fixtures.CHAT_ID, fixtures.BUYER_ID, "AI reply", before_attempt=verify)
        agent.send_msg.assert_not_awaited()

    async def test_pacing_queue_rechecks_takeover_and_manual_send_is_allowed(self):
        self.enable_knowledge_mode()
        agent, _ = self.build_agent(mode="rules_ai")
        agent.send_text_reliably = XianyuLive.send_text_reliably.__get__(agent)
        agent.connection_ready.set()
        agent.ws = Mock()
        agent.send_msg = AsyncMock()
        async def queued(operation):
            agent.enter_manual_mode(fixtures.CHAT_ID)
            return await operation()
        agent._send_with_pacing = queued
        with self.assertRaises(ManualTakeoverError):
            await agent.send_text_reliably(fixtures.CHAT_ID, fixtures.BUYER_ID, "AI reply")
        agent.send_msg.assert_not_awaited()
        await agent.send_text_reliably(fixtures.CHAT_ID, fixtures.BUYER_ID, "人工回复", allow_manual=True)
        agent.send_msg.assert_awaited_once()


class ManualReplyKeepsTakeoverTests(unittest.TestCase):
    setUp = manual_fixtures.ManualOutboxTests.setUp
    tearDown = manual_fixtures.ManualOutboxTests.tearDown
    enqueue = manual_fixtures.ManualOutboxTests.enqueue
    build_agent = manual_fixtures.ManualOutboxTests.build_agent

    def test_manual_send_does_not_clear_pending_human_in_knowledge_mode(self):
        self.enqueue()
        agent = self.build_agent()
        agent.knowledge_only = True
        self.manager.set_conversation_needs_human("chat-1", True)
        async def send(*_args, **kwargs):
            await kwargs["before_attempt"]()
        agent.send_text_reliably = send
        self.assertEqual(asyncio.run(agent.process_manual_outbox_once()), "acknowledged")
        self.assertTrue(self.manager.conversation_needs_human("chat-1"))
        self.assertTrue(agent.is_manual_mode("chat-1"))


class KnowledgeVersionClientTests(unittest.TestCase):
    make_bot = client_fixtures.LLMResilienceTests.make_bot

    def test_reply_parsing_retains_and_validates_knowledge_version(self):
        payload = {"decision": "reply", "reply": "材质答案", "reason_code": "material_answered", "sources": ["material_excel"], "knowledge_status": "matched", "knowledge_version": "a" * 64}
        bot, _ = self.make_bot(payload)
        self.assertEqual(bot.generate_reply_result("鞋面材质")["knowledge_version"], "a" * 64)
        for bad in ("", "not-a-version", 7, "A" * 64):
            bot, _ = self.make_bot({**payload, "knowledge_version": bad})
            with self.assertRaises(LLMResponseFormatError):
                bot.generate_reply_result("鞋面材质")

    def test_readiness_sends_version_and_rejects_changed_or_missing_version(self):
        bot, session = self.make_bot()
        response = Mock(status_code=200, content=b"{}")
        response.json.return_value = {"ok": True, "config_revision": 7, "knowledge_version": "a" * 64}
        session.post.return_value = response
        self.assertEqual(bot.ensure_ready(7, "a" * 64), 7)
        self.assertEqual(session.post.call_args.kwargs["json"], {"expected_config_revision": 7, "expected_knowledge_version": "a" * 64})
        response.json.return_value = {"ok": True, "config_revision": 7, "knowledge_version": "b" * 64}
        with self.assertRaises(LLMNotReadyError):
            bot.ensure_ready(7, "a" * 64)
        response.json.return_value = {"ok": True, "config_revision": 7}
        with self.assertRaises(LLMResponseFormatError):
            bot.ensure_ready(7, "a" * 64)
        with self.assertRaises(LLMConfigurationError):
            bot.ensure_ready(7, "invalid")


class CustomerServiceOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_entrypoints_are_noops_without_platform_or_inventory_access(self):
        agent = object.__new__(XianyuLive)
        agent.customer_service_only = True
        agent._call_xianyu_api = AsyncMock()
        agent._refresh_runtime_config = Mock()
        agent.delivery_store = Mock()
        agent.parse_paid_order_event = Mock(return_value={"order_key": "synthetic"})
        self.assertTrue(await agent.handle_paid_order({}))
        await agent.retry_pending_deliveries()
        await agent.scan_pending_orders()
        await agent._pending_order_scan_loop()
        await agent._reconcile_pending_order({})
        await agent._fulfill_order("order", {})
        await agent._ship_platform_order("order", None)
        self.assertIsNone(await agent._create_order_conversation("buyer", "item"))
        agent._call_xianyu_api.assert_not_awaited()
        agent._refresh_runtime_config.assert_not_called()
        self.assertEqual(agent.delivery_store.mock_calls, [])
        coroutine = agent.retry_pending_deliveries()
        self.assertIsNone(agent._schedule_delivery_task(coroutine))
        self.assertIsNone(coroutine.cr_frame)


if __name__ == "__main__":
    unittest.main()
