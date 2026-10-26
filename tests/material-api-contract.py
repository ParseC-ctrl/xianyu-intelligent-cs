#!/usr/bin/env python3
"""Authenticated material upload/preview contracts; no real platform or model."""
import base64
import hashlib
import io
import time
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
import threading
import openpyxl

ROOT = Path(__file__).resolve().parents[1]
temp = tempfile.TemporaryDirectory(dir="/private/tmp" if sys.platform == "darwin" else None)
run = Path(temp.name).resolve()
os.environ.update({"SAAS_ENV": "development", "SAAS_TESTING": "1", "SAAS_DB": str(run / "api.db"),
                   "SAAS_TENANTS_DIR": str(run / "tenants"), "SAAS_BOT_ROOT": str(run / "no-worker"),
                   "SAAS_AI_MASTER_KEY": base64.b64encode(b"x" * 32).decode(),
                   "SAAS_PUBLIC_ORIGIN": "http://testserver", "SAAS_TRUSTED_HOSTS": "testserver",
                   "SAAS_ALLOW_REGISTRATION": "1", "SAAS_KNOWLEDGE_ONLY": "1",
                   "SAAS_UPDATE_MAINTENANCE_FILE": str(run / "maintenance.json"),
                   "SAAS_CUSTOMER_SERVICE_ONLY": "1", "SAAS_DISABLE_UPSTREAM_UPDATES": "1"})
sys.path.insert(0, str(ROOT / "backend"))
from fastapi.testclient import TestClient
import app
from job_consumer import JobConsumer
REAL_START_SERVICES = app.start_services


def main():
    headers = {"Origin": "http://testserver", "X-SaaS-Browser-Intent": "browser-write"}
    book = openpyxl.Workbook(); sheet = book.active
    sheet.append(["主商品编码", "货号", "颜色", "鞋面", "内里", "鞋垫", "鞋底"])
    sheet.append(["TEST100", "TEST100-01", "黑色", "材质甲", "织物", "织物", "橡胶"])
    sheet.append(["TEST100", "TEST100-02", "白色", "材质乙", "织物", "织物", "橡胶"])
    data = io.BytesIO(); book.save(data); book.close()
    # Maintenance's executor-owned root directory is tested separately on
    # Linux. This upload/auth test runs as a normal macOS developer as well.
    with patch.object(app, "maintenance_active", return_value=False), patch.object(app, "start_services"), TestClient(app.app) as client:
        assert client.get("/api/bot/materials/status").status_code == 401
        registration = client.post("/api/auth/register", json={"username": "material-admin", "password": "fixture-material-password"}, headers=headers)
        assert registration.status_code == 200, registration.json()
        login = client.post("/api/auth/login", json={"username": "material-admin", "password": "fixture-material-password"}, headers=headers)
        assert login.status_code == 200
        client.cookies.set("xianyu_saas_session", login.cookies.get("xianyu_saas_session"), path="/")
        status = client.get("/api/bot/materials/status").json()
        assert client.get("/api/auth/capabilities").json()["customer_service_only"] is True
        assert client.get("/api/bot/catalog").status_code == 200
        assert client.get("/api/bot/catalog/settings").json()["settings"]["enabled"] is False
        assert client.post("/api/bot/catalog/pause", headers=headers).json()["paused"] is True
        assert client.get("/api/bot/catalog").json()["paused"] is True
        assert client.post("/api/bot/catalog/sync", json={}, headers=headers).status_code == 409
        assert client.post("/api/bot/catalog/sync", json={"item_ids": []}, headers=headers).status_code == 422
        assert client.post("/api/bot/catalog/resume", headers=headers).status_code == 400  # no Cookie yet
        assert client.get("/api/bot/catalog").json()["paused"] is False
        assert client.post("/api/bot/catalog/sync", json={"item_ids": ["../other"]}, headers=headers).status_code == 400
        account_for_schedule = app.db.get_shop_account(1, account_key="default")
        blocked = client.put("/api/bot/catalog/settings", json={"enabled": True, "interval_hours": 6}, headers=headers)
        assert blocked.status_code == 409
        app.write_secret(1, "catalog_sync_settings.json", json.dumps({"enabled": True, "interval_hours": 6, "last_enqueued_at": 1}), "default")
        assert client.get("/api/bot/catalog/settings").json()["settings"]["enabled"] is False
        with patch.object(app, "_enqueue_catalog_sync") as enqueue:
            app._catalog_schedule_once(now=time.time() + 7 * 3600)
            enqueue.assert_not_called()
        app.write_secret(1, "catalog_sync_settings.json", json.dumps({"enabled": False, "interval_hours": 6, "last_enqueued_at": None}), "default")
        with patch.dict(os.environ, {"SAAS_CUSTOMER_SERVICE_ONLY": "0"}):
            scheduled = client.put("/api/bot/catalog/settings", json={"enabled": True, "interval_hours": 6}, headers=headers)
            assert scheduled.status_code == 200
            assert client.put("/api/bot/catalog/settings", json={"enabled": True, "interval_hours": 7}, headers=headers).status_code == 400
            assert client.put("/api/bot/catalog/settings", json={"enabled": True, "interval_hours": 6, "cookies": "not-accepted"}, headers=headers).status_code == 422
            schedule_row = {"user_id": 1, "account_id": account_for_schedule["id"], "desired_state": "running"}
            with patch.object(app.db, "list_worker_runtimes", return_value=[schedule_row]), patch.object(app, "_enqueue_catalog_sync") as enqueue:
                app._catalog_schedule_once(now=time.time())
                enqueue.assert_not_called()
                app._catalog_schedule_once(now=time.time() + 7 * 3600)
                enqueue.assert_called_once()
            with patch.object(app.db, "list_worker_runtimes", return_value=[{**schedule_row, "desired_state": "stopped"}]), patch.object(app, "_enqueue_catalog_sync") as enqueue:
                app._catalog_schedule_once(now=time.time() + 7 * 3600)
                enqueue.assert_not_called()
            assert client.post("/api/bot/catalog/pause", headers=headers).status_code == 200
            with patch.object(app.db, "list_worker_runtimes", return_value=[schedule_row]), patch.object(app, "_enqueue_catalog_sync") as enqueue:
                app._catalog_schedule_once(now=time.time() + 7 * 3600)
                enqueue.assert_not_called()
            assert client.post("/api/bot/catalog/resume", headers=headers).status_code == 400
            with patch.object(app, "is_platform_admin", return_value=False):
                assert client.get("/api/bot/catalog/settings").status_code == 403
                assert client.put("/api/bot/catalog/settings", json={"enabled": False, "interval_hours": 24}, headers=headers).status_code == 403
        assert client.put("/api/bot/catalog/settings", json={"enabled": False, "interval_hours": 24}, headers=headers).status_code == 200
        # Historical cookie failures must terminate the old browser job lock
        # without declaring the replacement login expired.
        old_cookie = "unb=900001; _m_h5_tk=old-fixture_tail"
        new_cookie = "unb=900001; _m_h5_tk=new-fixture_tail"
        normalized, _ = app.parse_cookie_header(old_cookie)
        old_hash = hashlib.sha256(normalized.encode()).hexdigest()[:24]
        old_row = app.db.enqueue_job(1, "shop_sync", "catalog-old-fixture", account_id=account_for_schedule["id"],
            payload={"include_details": True, "cookie_fingerprint": old_hash}, max_attempts=1)
        failed_row = {**dict(old_row), "status": "dead_letter", "last_error_code": "cookie_expired"}
        real_read = app.read_secret
        def read_new(uid, name, key="default"):
            return new_cookie if name == "cookies.txt" else real_read(uid, name, key)
        with patch.object(app, "read_secret", side_effect=read_new):
            try:
                app._async_job_response(failed_row, account_for_schedule)
                raise AssertionError("terminal job must report its final state")
            except app.HTTPException as error:
                assert error.detail["terminal"] and error.detail["historical"]
                assert error.detail["job_status"] == "dead_letter" and error.detail["job_id"] == old_row["id"]
                assert error.detail["code"] == "job_superseded"
                assert old_cookie not in json.dumps(error.detail) and new_cookie not in json.dumps(error.detail)
            identity = client.get("/api/bot/catalog").json()["account_identity"]
            assert len(identity) == 24 and identity != old_hash
            next_job = app._enqueue_catalog_sync(1, account_for_schedule)
            assert next_job["id"] != old_row["id"], "new login must not reuse a queued old-cookie job"
        progress = {"version": 1, "job_id": next_job["id"], "account_id": account_for_schedule["id"],
            "account_generation": int(account_for_schedule["generation"] or 0), "cookie_fingerprint": identity,
            "phase": "details", "status": "running", "total": 4, "completed": 2, "succeeded": 2,
            "failed": 0, "remaining": 2, "batch_total": 4, "batch_completed": 2, "remaining_after_batch": 0,
            "eta_seconds": 14, "eta_confidence": "estimated", "started_at": time.time(), "updated_at": time.time()}
        app.write_secret(1, "catalog_progress.json", json.dumps(progress), "default")
        with patch.object(app, "read_secret", side_effect=read_new):
            observed = client.get("/api/bot/catalog/progress", params={"job_id": next_job["id"]})
            assert observed.status_code == 200 and observed.json()["progress"]["completed"] == 2
            assert "cookie_fingerprint" not in observed.text and "account_generation" not in observed.text
            assert client.get("/api/bot/catalog/progress", params={"job_id": old_row["id"]}).json()["progress"] is None
            assert client.get("/api/bot/catalog/progress", params={"job_id": 999999}).status_code == 404
            assert client.post("/api/bot/catalog/sync", json={"refresh_all": "yes"}, headers=headers).status_code == 422
            assert client.post("/api/bot/catalog/sync", json={"refresh_all": True, "item_ids": ["100"]}, headers=headers).status_code == 400
            assert client.post("/api/bot/catalog/sync", json={"refresh_all": True}, headers=headers).status_code == 409
        def read_old(uid, name, key="default"):
            return old_cookie if name == "cookies.txt" else real_read(uid, name, key)
        with patch.object(app, "read_secret", side_effect=read_old):
            assert client.get("/api/bot/catalog/progress", params={"job_id": next_job["id"]}).json()["progress"] is None
        with patch.object(app, "read_secret", return_value=old_cookie):
            try:
                app._async_job_response(failed_row, account_for_schedule)
            except app.HTTPException as error:
                assert error.detail["terminal"] and not error.detail["historical"]
                assert error.detail["code"] == "cookie_expired"
        with app.db._lock:
            app.db.con.execute("UPDATE jobs SET status='dead_letter' WHERE id IN (?,?)", (old_row["id"], next_job["id"]))
            app.db.con.commit()
        app._sync_account_state(1, "risk_control", snapshot={"products": [], "account_ref": "fixture"}, account=account_for_schedule)
        assert app.db.get_shop_account(1, account_key="default")["last_error_code"] == "risk_control"
        with patch.object(app, "identify_scope", return_value=(1, "default")):
            blocked = client.post("/internal/v1/ai/ready", json={"expected_config_revision": 0}, headers={"Authorization": "Bearer fixture", "X-Shop-Account": "default"})
            assert blocked.status_code == 409
        assert app._shop_sync_payload({"catalog_sync": {"stopped_reason": "risk_control"}})["connected"] is False
        app.db.set_worker_desired(1, True, mode="rules_ai", account_id=account_for_schedule["id"])
        app.db.set_worker_auto_resume(1, account_for_schedule["id"], 1)
        with patch.object(app, "_stop_account_worker_locked", return_value=(True, "stopped")) as stop:
            app._catalog_runtime_protection_once()
            stop.assert_called_once()
        protected = app.db.get_worker_runtime(1, account_for_schedule["id"])
        assert protected["desired_state"] == "stopped" and protected["auto_resume"] == 0
        app.db.update_shop_account(1, account_id=account_for_schedule["id"], status="ready", last_error_code="")
        assert client.get("/api/version/public").status_code == 200
        # Dedicated profile blocks handlers even through the public URL alias;
        # no hidden UI/forged request can launch operations or fulfilment.
        with patch.object(app, "get_orders", side_effect=AssertionError("orders handler ran")), patch.object(app, "save_products", side_effect=AssertionError("fulfilment handler ran")):
            for path, method in (("/api/bot/orders", "get"), ("/api/bot/cards", "get"),
                                 ("/api/bot/templates", "get"), ("/api/bot/products", "put"),
                                 ("/api/automation", "put"), ("/api/admin/updates/check", "post"),
                                 ("/api/ops/chat", "post"), ("/xianyu-intelligent-cs/api/bot/products/batch/commit", "post")):
                blocked = getattr(client, method)(path, headers=headers)
                assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "customer_feature_disabled", (path, blocked.status_code)
        assert status["knowledge_only"] and status["code_count"] == 0
        assert client.post("/api/bot/start", headers=headers).status_code == 400
        assert client.post("/api/bot/start", json={"mode": "rules"}, headers=headers).status_code == 400
        account = app.db.get_shop_account(1, account_key="default")
        initial_settings = dict(app._read_automation_settings(1, "default"))
        reply_settings = {"delay_min_seconds": 2, "delay_max_seconds": 3, "business_hours_enabled": False, "business_start": "09:00", "business_end": "23:00"}
        with patch.object(app, "_enqueue_shop_refresh", side_effect=AssertionError("settings triggered platform sync")):
            assert client.get("/api/bot/reply-settings").status_code == 200
            saved_settings = client.put("/api/bot/reply-settings", json=reply_settings, headers=headers)
            assert saved_settings.status_code == 200 and saved_settings.json()["settings"] == reply_settings, saved_settings.json()
        current_settings = app._read_automation_settings(1, "default")
        assert all(current_settings[key] == value for key, value in initial_settings.items() if key not in reply_settings)
        assert client.put("/api/bot/reply-settings", json={**reply_settings, "delay_min_seconds": 61}, headers=headers).status_code == 422
        assert client.put("/api/bot/reply-settings", json={**reply_settings, "enabled": False}, headers=headers).status_code == 422
        with patch.object(app, "is_platform_admin", return_value=False):
            assert client.get("/api/bot/reply-settings").status_code == 403
        original_name = account["display_name"]
        with patch.object(app, "_enqueue_shop_refresh", side_effect=AssertionError("rename requested platform sync")):
            renamed = client.patch("/api/bot/accounts/default", json={"name": "客服工作台备注"}, headers=headers)
            assert renamed.status_code == 200 and renamed.json()["account"]["name"] == "客服工作台备注"
            assert app.db.get_shop_account(1, account_key="default")["display_name"] == "客服工作台备注"
            assert client.patch("/api/bot/accounts/default", json={"name": original_name}, headers=headers).status_code == 200
        with patch.object(app, "_start_account_worker_locked", side_effect=AssertionError("login autostarted")):
            assert app._autostart_account_worker(1, account)["code"] == "explicit_ai_start_required"
        with patch.object(app, "_start_account_worker_locked", side_effect=AssertionError("restored rules mode")):
            app.restore_desired_workers()
        runtime = app.db.get_worker_runtime(1, account["id"])
        assert runtime["desired_state"] == "stopped" and runtime["auto_resume"] == 0
        app.db.set_worker_auto_resume(1, account["id"], 1)
        app.db.set_worker_desired(1, True, mode="rules_ai", account_id=account["id"])
        with patch.object(app, "_stop_account_worker_locked", side_effect=RuntimeError("fixture stop failure")):
            assert client.post("/api/bot/stop", headers=headers).status_code == 503
        runtime = app.db.get_worker_runtime(1, account["id"])
        assert runtime["desired_state"] == "stopped" and runtime["auto_resume"] == 0
        result = client.post("/api/bot/materials/import?filename=fixture.xlsx", content=data.getvalue(), headers={**headers, "Content-Type": "application/octet-stream"})
        assert result.status_code == 200, result.json()
        assert result.json()["code_count"] == 2
        previous = client.get("/api/bot/materials/status").json()["version"]
        bad = client.post("/api/bot/materials/import?filename=fixture.xlsx", content=b"invalid", headers=headers)
        assert bad.status_code == 400
        assert client.get("/api/bot/materials/status").json()["version"] == previous
        assert client.get("/api/bot/materials/search?q=TEST100").json()["records"][0]["upper"] == "材质甲"
        preview = client.post("/api/bot/materials/preview", json={"code": "TEST100-01", "message": "鞋面是什么?"}, headers=headers)
        assert preview.status_code == 200 and preview.json()["sent"] is False
        assert preview.json()["reply"] == "这款鞋的鞋面材质是材质甲。", preview.json()
        assert client.post("/api/bot/materials/preview", json={"code": "TEST100", "color": "白色", "message": "鞋面是什么?"}, headers=headers).json()["reply"] == "这款鞋的鞋面材质是材质乙。"
        assert client.post("/api/bot/materials/preview", json={"code": "TEST100-01", "message": "几天发货?"}, headers=headers).json()["decision"] == "handoff"
        assert client.post("/api/bot/materials/preview", json={"code": "TEST100-01", "message": "转人工"}, headers=headers).json()["reason_code"] == "buyer_requests_human"
        assert client.post("/api/bot/materials/preview", json={"message": "你好"}, headers=headers).json()["decision"] == "reply"
        assert client.post("/api/bot/materials/preview", json={"message": "天气怎么样"}, headers=headers).json()["decision"] in {"reply", "no_reply"}
        assert client.get("/api/bot/materials/faqs").json()["faqs"] == []
        faq = {"id": "invoice", "question": "可以开发票吗？", "answer": "本店目前不提供发票。", "enabled": False, "topics": ["发票"]}
        assert client.put("/api/bot/materials/faqs", json={"faqs": [faq]}, headers=headers).status_code == 200
        assert client.post("/api/bot/materials/preview", json={"message": "能开发票吗"}, headers=headers).json()["decision"] == "handoff"
        saved = client.put("/api/bot/materials/faqs", json={"faqs": [{**faq, "enabled": True}]}, headers=headers)
        assert saved.status_code == 200 and len(saved.json()["knowledge_version"]) == 64
        approved = client.post("/api/bot/materials/preview", json={"message": "可以开发票吗"}, headers=headers).json()
        assert approved["decision"] == "reply" and approved["reply"] == faq["answer"], approved
        assert client.get("/api/bot/materials/faqs", headers={"X-Shop-Account": "someone-else"}).status_code in {403, 404}
        unsafe = client.put("/api/bot/materials/faqs", json={"faqs": [{**faq, "answer": "API_KEY=fixture-unsafe-value"}]}, headers=headers)
        assert unsafe.status_code == 400
        assert client.get("/api/bot/materials/faqs").json()["faqs"][0]["answer"] == faq["answer"]
        merged_faq = {"id": "deploy-fixture", "question": "合成部署问题", "answer": "合成部署回答", "topics": [], "enabled": True, "fixed_reply": True}
        merged_response = client.post("/api/bot/materials/faqs/merge", json={"faqs": [merged_faq]}, headers=headers)
        assert merged_response.status_code == 200, merged_response.text
        assert {row["id"] for row in merged_response.json()["faqs"]} == {"invoice", "deploy-fixture"}
        again = client.post("/api/bot/materials/faqs/merge", json={"faqs": [merged_faq]}, headers=headers)
        assert again.json()["knowledge_version"] == merged_response.json()["knowledge_version"]
        foreign = client.post("/api/bot/materials/faqs/merge", json={"faqs": [merged_faq]}, headers={**headers, "X-Shop-Account": "someone-else"})
        assert foreign.status_code in {403, 404}
        invalid = client.post("/api/bot/materials/faqs/merge", json={"faqs": [merged_faq, merged_faq]}, headers=headers)
        assert invalid.status_code == 400
        assert len(client.get("/api/bot/materials/faqs").json()["faqs"]) == 2
        client.put("/api/bot/materials/faqs", json={"faqs": [{**faq, "enabled": True}]}, headers=headers)
        assert client.get("/api/bot/materials/status", headers={"X-Shop-Account": "someone-else"}).status_code in {403, 404}
        account = app.db.get_shop_account(1, account_key="default")
        path = Path(app.ai_service.runtime_paths(1, account["id"], "default")["products_snapshot_file"])
        path.write_text(json.dumps({"products": [{"id": "100", "status": "在售", "title": "测试鞋", "description": "商品货号 TEST100-01"}]})); path.chmod(0o600)
        active_fixture = path.read_text()
        path.write_text(json.dumps({"products": [{"id": "100", "status": "已售出", "title": "测试鞋", "description": "TEST100-01"}]}))
        assert client.post("/api/bot/materials/preview", json={"item_id": "100", "message": "鞋底呢?"}, headers=headers).status_code == 404
        path.write_text(active_fixture)
        automatic = client.get("/api/bot/materials/binding/100").json()
        assert automatic["source"] == "product_details" and automatic["binding"]["code"] == "TEST100-01"
        assert app.ai_service.material_store(1, account["id"], "default").product_binding("100") is None
        assert client.post("/api/bot/materials/preview", json={"item_id": "100", "message": "鞋底呢?"}, headers=headers).json()["reply"] == "这款鞋的鞋底材质是橡胶。"
        path.write_text(json.dumps({"products": [{"id": "100", "status": "在售", "title": "测试鞋", "description": "TEST100-01 TEST100-02"}]}))
        assert client.get("/api/bot/materials/binding/100").json()["binding"] is None
        path.write_text(json.dumps({"products": [{"id": "100", "status": "在售", "title": "测试鞋"}]})); path.chmod(0o600)
        assert client.get("/api/bot/materials/binding/100").json()["binding"] is None
        binding = client.put("/api/bot/materials/binding/100", json={"code": "TEST100-01"}, headers=headers)
        assert binding.status_code == 200, binding.json()
        assert client.get("/api/bot/materials/binding/100").json()["binding"]["code"] == "TEST100-01"
        assert client.post("/api/bot/materials/preview", json={"item_id": "100", "message": "鞋底呢?"}, headers=headers).json()["reply"] == "这款鞋的鞋底材质是橡胶。"
        assert client.put("/api/bot/materials/binding/999", json={"code": "TEST100-01"}, headers=headers).status_code == 404
        assert client.get("/api/admin/version").status_code == 403
        # A queued legacy operations task cannot invoke any model/platform
        # service after switching to customer-only deployment.
        consumer = JobConsumer.__new__(JobConsumer)
        consumer.stop_event = threading.Event()
        consumer.maintenance_func = lambda: False
        consumer._fail = Mock(return_value=True)
        consumer._process_ops = Mock(side_effect=AssertionError("operations executed"))
        assert consumer._process({"id": 9, "kind": "ops_run"}) == "failed"
        consumer._process_ops.assert_not_called()
        assert consumer._fail.call_args.args[1] == "customer_feature_disabled"
        # Dedicated startup has no automatic update network tasks.
        with patch.dict(os.environ, {"SAAS_TESTING": "0"}), patch.object(app, "_services_started", False), patch.object(app, "_watchdog_thread", object()), patch.object(app, "maintenance_watcher") as watcher, patch.object(app, "update_api") as updater, patch.object(app, "update_probe") as probe, patch.object(app, "restore_desired_workers"):
            REAL_START_SERVICES()
            watcher.start.assert_called_once()
            updater.start.assert_not_called()
            probe.start.assert_not_called()
    temp.cleanup()
    print("material API contract: authorization, isolation, upload rollback, exact preview and product binding passed")


if __name__ == "__main__":
    main()
