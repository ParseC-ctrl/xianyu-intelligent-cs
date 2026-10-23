#!/usr/bin/env python3
"""Offline API/worker takeover compatibility using synthetic account state."""
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "worker"))

import records
from account_storage import AccountStorage
from context_manager import ChatContextManager
from delivery_store import DeliveryStore


class ManualCustomerServiceContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch_root = patch.object(records, "TENANTS_ROOT", self.temp.name)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.storage = AccountStorage(self.temp.name)
        self.root = self.storage.ensure_account_dir(1, "default")
        self.storage.write_text(1, "default", "cookies.txt", "unb=123; synthetic=offline")
        self.context = ChatContextManager(db_path=str(self.root / "chat_history.db"))
        self.context.add_message_by_chat("chat", "buyer", "1234", "user", "synthetic question", source_id="buyer:1")
        self.context.set_customer_conversation_scope("chat", "1234", hashlib.sha256(b"123").hexdigest()[:16], "owned")
        self.store = DeliveryStore(str(self.root / "delivery_state.db"))

    def test_customer_history_is_persistent_and_item_scoped(self):
        self.context.add_message_by_chat("chat", "buyer", "5678", "user", "another item", source_id="buyer:2")
        self.context.add_message_by_chat("other-chat", "other-buyer", "1234", "user", "another buyer", source_id="buyer:3")
        restarted = ChatContextManager(db_path=str(self.root / "chat_history.db"))
        rows = restarted.get_context_by_chat("chat", item_id="1234")
        self.assertEqual([row["content"] for row in rows if row["role"] == "user"], ["synthetic question"])
        self.assertEqual(len(restarted.get_context_by_chat("chat")), 2)

    def test_persistent_api_takeover_works_after_restart_and_resume_clears_flags(self):
        with patch.dict(os.environ, {"SAAS_KNOWLEDGE_ONLY": "1"}):
            row = records.set_conversation_takeover(1, "chat", True)
            self.assertTrue(row["takeover"])
            self.assertIsNone(row["takeover_expires_at"])
            self.context.set_conversation_needs_human("chat", True)
            restarted = DeliveryStore(str(self.root / "delivery_state.db"), now_fn=lambda: 1e10)
            self.assertTrue(restarted.is_manual_mode("chat"))
            row = records.set_conversation_takeover(1, "chat", False)
            self.assertFalse(row["takeover"])
            self.assertFalse(row["needs_human"])
            self.assertFalse(restarted.is_manual_mode("chat"))

    def test_api_intervention_invalidates_queued_handoff_without_resuming_ai(self):
        token = self.store.claim_handoff("chat")
        self.context.set_conversation_needs_human("chat", True)
        with patch.dict(os.environ, {"SAAS_KNOWLEDGE_ONLY": "1"}):
            row = records.set_conversation_takeover(1, "chat", True)
        self.assertTrue(row["takeover"])
        self.assertTrue(row["needs_human"])
        self.assertFalse(self.store.handoff_notice_current("chat", token))
        self.assertTrue(self.store.is_manual_mode("chat"))

    def test_default_api_remains_timed_and_unknown_chat_cannot_be_toggled(self):
        with patch.dict(os.environ, {"SAAS_KNOWLEDGE_ONLY": "0"}):
            row = records.set_conversation_takeover(1, "chat", True)
        self.assertTrue(row["takeover"])
        self.assertGreater(row["takeover_expires_at"], 0)
        self.assertIsNone(records.set_conversation_takeover(1, "unknown", True))
        self.assertFalse(self.store.is_manual_mode("unknown"))


if __name__ == "__main__":
    unittest.main()
