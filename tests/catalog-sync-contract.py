#!/usr/bin/env python3
"""Synthetic HTTP/catalogue and durable consumer contracts; no marketplace I/O."""
import hashlib
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="catalog-contract-")
RUN = Path(TEMP.name).resolve()
os.environ.update({"SAAS_DB": str(RUN / "control.db"), "SAAS_TENANTS_DIR": str(RUN / "tenants"),
                   "SAAS_TESTING": "1", "SAAS_SHOP_SYNC_COOLDOWN_SECONDS": "1"})
sys.path.insert(0, str(ROOT / "backend"))
import shop_sync
import catalog_sync
from account_storage import AccountStorage
from catalog_sync import refresh_catalog_details, merge_cached_details, DETAIL_API
from db import DB
from job_consumer import JobConsumer

COOKIE = "unb=900001; _m_h5_tk=fixture-catalog_tail"
REFERENCE = shop_sync.account_ref(shop_sync.parse_cookie_header(COOKIE)[1])


def success(data):
    return {"ret": ["SUCCESS::调用成功"], "data": data}


def product(identifier="100"):
    return shop_sync.extract_product({"id": identifier, "title": "测试商品", "price": "2.00", "itemStatus": "0"}, "2026-10-14T00:00:00+0000")


class Clock:
    def __init__(self): self.value, self.sleeps = 0.0, []
    def now(self): return self.value
    def sleep(self, seconds): self.sleeps.append(seconds); self.value += seconds


def helper_contract():
    clock, calls = Clock(), []
    def request(api, data, spm):
        calls.append((api, data["itemId"], clock.value))
        return success({"itemDO": {"itemId": data["itemId"], "userId": "900001", "title": "测试商品", "desc": "真实详情正文"}})
    rows, receipt = refresh_catalog_details([product("100"), product("101")], "900001", request,
        deadline=100, clock=clock.now, sleeper=clock.sleep)
    assert receipt["status"] == "ready" and receipt["ready"] == receipt["attempted"] == 2
    assert calls[1][2] - calls[0][2] >= 3.0, calls
    assert all(item["description"] == "真实详情正文" and item["description_source"] == "detail" for item in rows)
    assert all(item["detail_synced_at"] for item in rows)

    old = {**product(), "description": "旧有效详情", "description_source": "detail",
           "detail_sync_status": "ready", "detail_synced_at": "2026-10-13T00:00:00+00:00", "codes": ["SHOE100-01"]}
    snapshot = {"account_ref": REFERENCE, "products": [old]}
    cached = merge_cached_details([product()], snapshot, REFERENCE)
    assert cached[0]["description"] == old["description"] and cached[0]["detail_cached"]
    assert merge_cached_details([product()], snapshot, "different-account")[0]["description"] == ""
    modified = {**product(), "title": "另一货号 NEW999-01", "codes": ["NEW999-01"]}
    assert merge_cached_details([modified], snapshot, REFERENCE)[0]["description"] == ""
    invalid_old = {"account_ref": REFERENCE, "products": [{**old, "description_source": "list", "detail_sync_status": "failed"}]}
    assert merge_cached_details([product()], invalid_old, REFERENCE)[0]["description"] == ""
    rows, receipt = refresh_catalog_details(cached, "900001", lambda *_: success({"itemDO": {"itemId": "100", "title": "测试商品"}}),
        deadline=100, item_ids=["100"], clock=clock.now, sleeper=clock.sleep)
    assert receipt["failed"] == 1 and rows[0]["detail_error_code"] == "detail_missing"
    assert rows[0]["description"] == "旧有效详情" and rows[0]["detail_synced_at"] == old["detail_synced_at"]
    assert rows[0]["detail_sync_status"] == "failed"
    for new_body, expected in [("没有编码的新正文", []), ("新货号 NEW777-01", ["NEW777-01"])]:
        clock = Clock()
        refreshed, receipt = refresh_catalog_details(cached, "900001",
            lambda *_: success({"itemDO": {"itemId": "100", "desc": new_body}}),
            deadline=100, item_ids=["100"], clock=clock.now, sleeper=clock.sleep)
        assert refreshed[0]["codes"] == expected and "SHOE100-01" not in refreshed[0]["codes"]
        assert "merchant_code" not in refreshed[0]
    old_merchant = {"account_ref": REFERENCE, "products": [{**old, "merchant_code": "OLD123-01"}]}
    copied = merge_cached_details([product()], old_merchant, REFERENCE)
    assert copied[0]["merchant_code_source"] == "cached"
    clock = Clock()
    refreshed, _ = refresh_catalog_details(copied, "900001", lambda *_: success({"itemDO": {"itemId": "100", "desc": "新正文"}}),
        deadline=100, item_ids=["100"], clock=clock.now, sleeper=clock.sleep)
    assert "merchant_code" not in refreshed[0] and "OLD123-01" not in refreshed[0]["codes"]
    listed = shop_sync.extract_product({"id": "100", "title": "测试商品", "merchantCode": "LIST123-01"}, "")
    clock = Clock()
    refreshed, _ = refresh_catalog_details([listed], "900001", lambda *_: success({"itemDO": {"itemId": "100", "desc": "新正文", "merchantCode": "DETAIL456-01"}}),
        deadline=100, clock=clock.now, sleeper=clock.sleep)
    assert refreshed[0]["merchant_code"] == "LIST123-01" and "DETAIL456-01" not in refreshed[0]["codes"]

    clock, calls = Clock(), []
    def slow(api, data, spm):
        calls.append(data["itemId"]); clock.value += 2
        return success({"itemDO": {"itemId": data["itemId"], "desc": "详情"}})
    rows, receipt = refresh_catalog_details([product("100"), product("101")], "900001", slow,
        deadline=shop_sync.REQUEST_TIMEOUT + .1, clock=clock.now, sleeper=clock.sleep)
    assert calls == ["100"] and receipt["ready"] == 1 and receipt["pending"] == 1
    assert receipt["status"] == "partial" and receipt["stopped_reason"] == "budget_exhausted"

    clock, calls = Clock(), []
    def unavailable(api, data, spm):
        calls.append(data["itemId"])
        raise shop_sync.ShopSyncError("network_error", "合成网络错误")
    rows, receipt = refresh_catalog_details([product(str(100 + i)) for i in range(5)], "900001", unavailable,
        deadline=100, clock=clock.now, sleeper=clock.sleep)
    assert calls == ["100", "101", "102"] and receipt["attempted"] == receipt["failed"] == 3
    assert receipt["pending"] == 2 and receipt["stopped_reason"] == "network_error"

    clock, calls = Clock(), []
    def risk(api, data, spm):
        calls.append(data["itemId"])
        return {"ret": ["FAIL_SYS_USER_VALIDATE::RGV587"], "data": {}}
    with patch.object(shop_sync, "_trip_circuit") as trip:
        _, receipt = refresh_catalog_details([product("100"), product("101")], "900001", risk,
            deadline=100, clock=clock.now, sleeper=clock.sleep)
    assert calls == ["100"] and receipt["failed"] == 1 and receipt["pending"] == 1
    assert receipt["stopped_reason"] == "risk_control" and trip.call_count == 1

    clock = Clock()
    _, receipt = refresh_catalog_details([product("100"), product("101")], "900001",
        lambda *_: success({"itemDO": {"itemId": "other", "userId": "900002", "desc": "错误归属"}}),
        deadline=100, clock=clock.now, sleeper=clock.sleep)
    assert receipt["attempted"] == 1 and receipt["stopped_reason"] == "detail_identity_mismatch"

    clock, calls = Clock(), []
    sequence = ["network_error", "platform_error", "detail_missing", "network_error", "platform_error", "network_error"]
    def mixed(api, data, spm):
        code = sequence[len(calls)]; calls.append(data["itemId"])
        raise shop_sync.ShopSyncError(code, "合成失败")
    _, receipt = refresh_catalog_details([product(str(100+i)) for i in range(7)], "900001", mixed,
        deadline=100, clock=clock.now, sleeper=clock.sleep)
    assert len(calls) == 6 and receipt["failed"] == 6 and receipt["pending"] == 1


def active_only_contract():
    calls = []
    def request(api, data, spm):
        calls.append((api, data))
        if api == shop_sync.PROFILE_API:
            return success({"nick": "合成测试账号"})
        if api == DETAIL_API:
            return success({"itemDO": {"itemId": data["itemId"], "desc": "货号 DEMO10001-01"}})
        page = data["pageNumber"]
        cards = ([{"id": "1", "itemStatus": "1"}, {"id": "2", "itemStatusStr": "已售出"},
                  {"id": "3"}, {"id": "4", "itemStatus": "0", "itemStatusStr": "已售出"}]
                 if page == 1 else [{"id": "5", "title": "在卖", "itemStatus": "0"}])
        return success({"cardList": [{"cardData": c} for c in cards], "nextPage": page == 1})
    with patch.object(catalog_sync, "DETAIL_REQUEST_INTERVAL", 0):
        snapshot = shop_sync.sync_shop(COOKIE, request_func=request, include_details=True)
    assert [p["id"] for p in snapshot["products"]] == ["5"]
    assert [d["pageNumber"] for a, d in calls if a == shop_sync.PRODUCTS_API] == [1, 2]
    assert [d["itemId"] for a, d in calls if a == DETAIL_API] == ["5"]
    assert snapshot["product_count"] == 1 and snapshot["active_only"]
    clean = shop_sync.active_catalog_snapshot({"products": [product("5"), {**product("6"), "status": "已下架"}], "catalog_sync": {"pending": 2}})
    assert [p["id"] for p in clean["products"]] == ["5"] and clean["catalog_sync"]["pending"] == 1


def http_contract():
    calls = []
    class Response:
        def __init__(self, data): self.body = json.dumps(data, ensure_ascii=False).encode()
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, maximum): return self.body[:maximum]
    def fake_http(request, timeout):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        api = query["api"][0]
        data_value = urllib.parse.parse_qs(request.data.decode())["data"][0]
        data = json.loads(data_value)
        expected = hashlib.md5(f"fixture-catalog&{query['t'][0]}&{shop_sync.APP_KEY}&{data_value}".encode()).hexdigest()
        assert request.get_method() == "POST" and query["sign"][0] == expected
        calls.append((api, data))
        if api == shop_sync.PROFILE_API: return Response(success({"nick": "合成目录店", "userId": "900001"}))
        if api == shop_sync.PRODUCTS_API:
            return Response(success({"cardList": [{"cardData": {"id": "100", "title": "测试商品", "price": "2.00", "itemStatus": "0"}}], "nextPage": False}))
        assert api == DETAIL_API and data == {"itemId": "100"}
        return Response(success({"itemDO": {"itemId": "100", "userId": "900001", "title": "测试商品", "desc": "详情" * 11000 + " SHOE100-01", "merchantCode": "MERCHANT100", "price": "999.00"}}))
    with patch.object(shop_sync.urllib.request, "urlopen", side_effect=fake_http), patch.object(shop_sync, "_circuit_until", return_value=0), patch.object(catalog_sync, "DETAIL_REQUEST_INTERVAL", 0):
        legacy = shop_sync.sync_shop(COOKIE)
        assert [api for api, _ in calls] == [shop_sync.PROFILE_API, shop_sync.PRODUCTS_API]
        calls.clear()
        detailed = shop_sync.sync_shop(COOKIE, include_details=True)
        assert [api for api, _ in calls] == [shop_sync.PROFILE_API, shop_sync.PRODUCTS_API, DETAIL_API]
        assert detailed["catalog_sync"]["status"] == "ready"
        item = detailed["products"][0]
        assert item["description_source"] == "detail" and len(item["description"]) == 20000
        assert "SHOE100-01" in item["codes"] and "SHOE100-01" not in item["description"]
        assert item["merchant_code"] == "MERCHANT100" and item["price"] == "2.00"
        calls.clear()
        ordinary = shop_sync.sync_shop(COOKIE, previous_snapshot=detailed)
        assert len(calls) == 2 and ordinary["products"][0]["description"] == item["description"]
        assert ordinary["products"][0]["detail_sync_status"] == "ready"
    wide_title = shop_sync.extract_product({"id": "101", "title": "标题" * 100 + " SHOE777-01"}, "")
    assert "SHOE777-01" in wide_title["codes"] and "SHOE777-01" not in wide_title["title"]
    labeled = shop_sync.extract_product({"id": "102", "summary": "列表摘要", "itemLabelDataVO": {"labelData": {"x": {"tagList": [{"data": {"content": "包邮"}}]}}}}, "")
    assert labeled["description"] == "" and labeled["labels"] == ["包邮"]

    pages = []
    def lots(api, data, spm):
        if api == shop_sync.PROFILE_API: return success({"nick": "合成目录店"})
        if api == DETAIL_API: return success({"itemDO": {"itemId": data["itemId"], "desc": "详情"}})
        page = data["pageNumber"]; pages.append(page)
        return success({"cardList": [{"cardData": {"id": str(page*100+i), "title": "测试商品", "itemStatus": "0"}} for i in range(20)], "nextPage": True})
    with patch.object(catalog_sync, "DETAIL_REQUEST_INTERVAL", 0):
        detailed = shop_sync.sync_shop(COOKIE, request_func=lots, include_details=True)
    assert len(detailed["products"]) == 1000 and detailed["truncated"] is True and pages == list(range(1, 51))
    assert detailed["catalog_sync"]["requested"] == detailed["catalog_sync"]["ready"] == 20
    assert detailed["catalog_sync"]["remaining_pending"] == 980 and detailed["catalog_sync"]["stopped_reason"] == "batch_limit"
    def ended_at_limit(api, data, spm):
        result = lots(api, data, spm)
        if api == shop_sync.PRODUCTS_API:
            result["data"]["nextPage"] = data["pageNumber"] < 50
            if data["pageNumber"] == 1:
                result["data"]["topItem"] = {"id": "9999", "title": "额外置顶商品", "itemStatus": "0"}
        return result
    with patch.object(catalog_sync, "DETAIL_REQUEST_INTERVAL", 0):
        limited = shop_sync.sync_shop(COOKIE, request_func=ended_at_limit, include_details=True)
    assert len(limited["products"]) == 1000 and limited["truncated"] is True, "a final-page excess item must not be labelled complete"



def incremental_contract():
    clock, calls = Clock(), []
    def request(api, data, spm):
        calls.append((api, data))
        return success({"itemDO": {"itemId": data["itemId"], "desc": "详情"}})
    rows = [product(str(100+i)) for i in range(43)]
    first, receipt = refresh_catalog_details(rows, "900001", request, deadline=500, clock=clock.now, sleeper=clock.sleep)
    assert len(calls) == 20 and receipt["remaining_pending"] == 23 and receipt["requested"] == 20
    second, receipt = refresh_catalog_details(first, "900001", request, deadline=500, clock=clock.now, sleeper=clock.sleep)
    assert len(calls) == 40 and receipt["skipped_ready"] == 20 and receipt["remaining_pending"] == 3
    third, receipt = refresh_catalog_details(second, "900001", request, deadline=500, clock=clock.now, sleeper=clock.sleep)
    assert len(calls) == 43 and receipt["status"] == "ready"
    _, receipt = refresh_catalog_details(third, "900001", request, deadline=500, clock=clock.now, sleeper=clock.sleep)
    assert len(calls) == 43 and receipt["requested"] == 0
    failed, _ = refresh_catalog_details([product()], "900001", lambda *_: success({}), deadline=500, clock=clock.now, sleeper=clock.sleep)
    assert failed[0]["detail_sync_status"] == "failed" and not failed[0]["description"]
    merged = merge_cached_details([product()], {"account_ref": REFERENCE, "products": failed}, REFERENCE)
    _, receipt = refresh_catalog_details(merged, "900001", request, deadline=500, clock=clock.now, sleeper=clock.sleep)
    assert receipt["skipped_failed"] == 1 and receipt["attempted"] == 0
    _, receipt = refresh_catalog_details(merged, "900001", request, item_ids=["100"], deadline=500, clock=clock.now, sleeper=clock.sleep)
    assert receipt["attempted"] == 1
    label = {**product(), "labels": ["五分钟前发布"]}
    assert label["list_fingerprint"] == product()["list_fingerprint"]
    old = {**third[0]}
    old.pop("list_fingerprint")
    migrated = merge_cached_details([label], {"account_ref": REFERENCE, "products": [old]}, REFERENCE)
    assert migrated[0]["detail_sync_status"] == "ready" and migrated[0]["list_fingerprint"]
    changed = shop_sync.extract_product({"id": "100", "title": "测试商品", "price": "3.00", "itemStatus": "0"}, "")
    assert merge_cached_details([changed], {"account_ref": REFERENCE, "products": [third[0]]}, REFERENCE)[0]["detail_sync_status"] == "pending"
    base = {"id": "100", "title": "测试商品"}
    changed_time = shop_sync.extract_product({**base, "modifyTime": 1791936000000}, "")
    assert changed_time["list_fingerprint"] != shop_sync.extract_product(base, "")["list_fingerprint"]
    assert shop_sync.extract_product({**base, "modifyTime": "5分钟前"}, "")["list_modified_at"] == ""
    storage = AccountStorage(RUN / "pause-test")
    storage.ensure_account_dir(1, "default"); storage.ensure_account_dir(1, "other")
    assert not catalog_sync.catalog_sync_paused(storage, 1, "default")
    storage.write_text(1, "default", catalog_sync.CATALOG_CONTROL_NAME, '{"version":1,"paused":true}')
    assert catalog_sync.catalog_sync_paused(storage, 1, "default") and not catalog_sync.catalog_sync_paused(storage, 1, "other")
    storage.write_text(1, "default", catalog_sync.CATALOG_CONTROL_NAME, '{"version":1,"paused":"false"}')
    assert catalog_sync.catalog_sync_paused(storage, 1, "default")
    def pause(): raise shop_sync.ShopSyncError("catalog_paused", "暂停")
    paused, receipt = refresh_catalog_details([product()], "900001", request, deadline=500, before_request=pause, clock=clock.now, sleeper=clock.sleep)
    assert receipt["stopped_reason"] == "paused" and receipt["attempted"] == 0 and paused[0]["detail_sync_status"] == "pending"
    snapshot = {"version":1, "account_ref": REFERENCE, "nickname":"测试店", "products": first,
                "product_count":43, "synced_at":datetime.now(timezone.utc).isoformat(), "truncated":False}
    def cached_request(api, data, spm):
        assert api == DETAIL_API, "cached continuation must not fetch profile/list"
        return request(api, data, spm)
    with patch.object(catalog_sync, "DETAIL_REQUEST_INTERVAL", 0):
        resumed = shop_sync.sync_shop(COOKIE, request_func=cached_request, include_details=True, previous_snapshot=snapshot, resume_cached=True)
        forced = shop_sync.sync_shop(COOKIE, request_func=cached_request, include_details=True, previous_snapshot=snapshot, resume_cached=True, item_ids=["100"])
    assert resumed["catalog_sync"]["attempted"] == 20 and resumed["catalog_sync"]["remaining_pending"] == 3
    assert forced["catalog_sync"]["attempted"] == 1
    assert resumed["list_synced_at"] == snapshot["synced_at"]
    assert not catalog_sync.can_resume_snapshot({**snapshot, "list_synced_at":"2020-01-01T00:00:00+00:00"}, REFERENCE)
    paused_snapshot = shop_sync.sync_shop(COOKIE, request_func=lambda *_: (_ for _ in ()).throw(AssertionError("paused I/O")), include_details=True,
                                         previous_snapshot=snapshot, before_request=pause)
    assert paused_snapshot["products"] == snapshot["products"] and paused_snapshot["catalog_sync"]["stopped_reason"] == "paused"
    assert not catalog_sync.can_resume_snapshot({**snapshot, "account_ref":"other"}, REFERENCE, ["100"])
    assert not catalog_sync.can_resume_snapshot({**snapshot, "synced_at":"bad"}, REFERENCE)
    assert not catalog_sync.can_resume_snapshot({**snapshot, "synced_at":"2020-01-01T00:00:00+00:00"}, REFERENCE)
    for invalid in [[], ["../x"], ["100", "100"], [100], [str(100+i) for i in range(21)]]:
        try: catalog_sync.validate_item_ids(invalid)
        except shop_sync.ShopSyncError: pass
        else: raise AssertionError("invalid item ids accepted")
    # Inspect real shared pacing for profile and both list pages, with fake
    # time and a stub detail stage (no sleeps or marketplace traffic).
    timer, stamps = Clock(), []
    def listing(api, data, spm):
        stamps.append(timer.value)
        if api == shop_sync.PROFILE_API: return success({"nick":"店"})
        return success({"cardList":[{"id":str(data["pageNumber"]),"title":"测试"}],"nextPage": data["pageNumber"] == 1})
    with patch.object(shop_sync.time, "monotonic", timer.now), patch.object(shop_sync.time, "sleep", timer.sleep), patch.object(catalog_sync, "refresh_catalog_details", side_effect=lambda items,*args,**kwargs:(items,{"status":"partial"})):
        shop_sync.sync_shop(COOKIE, request_func=listing, include_details=True)
    assert stamps == [0, 3, 6], stamps


def image_contract():
    images = ["https://img.example.invalid/first.jpg", {"url":"//img.example.invalid/second.jpg"},
              {"imageUrl":"https://img.example.invalid/last.jpg"}]
    card = {"id":"100", "title":"测试商品", "images":images,
            "imageInfos":[{"picUrl":"https://img.example.invalid/second.jpg"}],
            "imageList":["javascript:alert(1)", "file:///private/key", "http://127.0.0.1/p", "http://[::1]/p", "http://localhost/p",
                         "https://user:pass@img.example.invalid/p", "https://img.example.invalid:bad/p", "https://img.example.invalid/white space", "https://img.example.invalid/\x00"]}
    expected = ["https://img.example.invalid/first.jpg", "https://img.example.invalid/second.jpg", "https://img.example.invalid/last.jpg"]
    extracted = shop_sync.extract_product(card, "")
    assert extracted["image_urls"] == expected
    long_images = [f"https://img.example.invalid/{i}.jpg" for i in range(25)]
    bounded = shop_sync.extract_product({"id":"100","images":long_images}, "")["image_urls"]
    assert len(bounded) == 20 and bounded == long_images[:19]+[long_images[-1]]
    clock = Clock()
    refreshed, _ = refresh_catalog_details([product()], "900001", lambda *_:success({"itemDO":{**card,"desc":"新正文"}}),
                                           deadline=100, clock=clock.now, sleeper=clock.sleep)
    assert refreshed[0]["image_urls"] == expected
    cached = {**refreshed[0], "sku_check":{"status":"matched", "selected_code":"PIC123-01"},
              "codes":["PIC123-01"], "codes_source":"sku_check"}
    previous = {"account_ref":REFERENCE,"products":[cached]}
    preserved = merge_cached_details([product()], previous, REFERENCE)
    assert preserved[0]["image_urls"] == expected and preserved[0]["sku_check"] == cached["sku_check"]
    assert preserved[0]["codes"] == ["PIC123-01"] and preserved[0]["codes_source"] == "sku_check"
    changed = shop_sync.extract_product({"id":"100","title":"测试商品","price":"3.00","itemStatus":"0"}, "")
    invalidated = merge_cached_details([changed], previous, REFERENCE)[0]
    assert "sku_check" not in invalidated and "PIC123-01" not in invalidated.get("codes",[])
    assert invalidated["detail_sync_status"] == "pending"
    updated, _ = refresh_catalog_details(preserved, "900001", lambda *_:success({"itemDO":{"itemId":"100","desc":"新正文"}}),
                                        item_ids=["100"], deadline=100, clock=clock.now, sleeper=clock.sleep)
    assert updated[0]["image_urls"] == [] and "sku_check" not in updated[0] and "PIC123-01" not in updated[0]["codes"]
    old_no_images = {**cached}; old_no_images.pop("image_urls")
    legacy = merge_cached_details([product()], {"account_ref":REFERENCE,"products":[old_no_images]}, REFERENCE)
    assert legacy[0]["detail_sync_status"] == "pending", "old ready caches need one image backfill"
    no_images_ready = {**cached, "image_urls":[]}
    assert merge_cached_details([product()], {"account_ref":REFERENCE,"products":[no_images_ready]}, REFERENCE)[0]["detail_sync_status"] == "ready"
    failed, _ = refresh_catalog_details(preserved, "900001", lambda *_:success({}), item_ids=["100"],
                                       deadline=100, clock=clock.now, sleeper=clock.sleep)
    assert failed[0]["image_urls"] == expected and failed[0]["sku_check"] == cached["sku_check"]


def progress_contract():
    now = datetime.now(timezone.utc)
    recent = {**product(), "description":"缓存正文", "description_source":"detail", "detail_sync_status":"ready", "detail_synced_at":now.isoformat()}
    stale = {**recent, "detail_synced_at":(now-timedelta(days=8)).isoformat()}
    failed = {**stale, "id":"999", "detail_sync_status":"failed"}
    assert catalog_sync.pending_details([recent], now=now)[0]["detail_sync_status"] == "ready"
    assert catalog_sync.pending_details([stale], now=now)[0]["detail_sync_status"] == "pending"
    assert catalog_sync.pending_details([failed], now=now)[0]["detail_sync_status"] == "failed"
    assert catalog_sync.pending_details([recent], refresh_all=True, now=now)[0]["detail_sync_status"] == "pending"
    assert merge_cached_details([product()], {"account_ref":REFERENCE,"products":[stale]}, REFERENCE)[0]["detail_sync_status"] == "pending"
    clock, events = Clock(), []
    ready = [{**recent,"id":str(100+i)} for i in range(43)] + [failed]
    def detail(api,data,spm):
        clock.value += 2
        return success({"itemDO":{"itemId":data["itemId"],"desc":"正文"}})
    rows, receipt = refresh_catalog_details(ready, "900001", detail, deadline=1000, refresh_all=True,
                                            progress=events.append, clock=clock.now, sleeper=clock.sleep)
    assert receipt["attempted"] == 20 and receipt["remaining_pending"] == 23 and rows[-1]["detail_sync_status"] == "failed"
    assert events[0]["total"] == 20 and events[0]["completed"] == 0
    assert events[-1]["completed"] == 20 and events[-1]["succeeded"] == 20 and events[-1]["failed"] == 0
    assert events[-1]["remaining"] == 0 and events[-1]["remaining_after_batch"] == 23
    assert all(event["phase"] == "details" and event["completed"] <= 20 for event in events)
    storage = AccountStorage(RUN / "progress-test")
    timer, active = Clock(), {"value":True}
    identity = {"job_id":7,"account_id":9,"account_generation":2,"cookie_fingerprint":"a"*24}
    def ensure():
        if not active["value"]: raise RuntimeError("identity changed")
    writer = catalog_sync.CatalogProgressWriter(storage, 1, "default", identity, ensure, clock=timer.now)
    catalog_sync.report_progress(writer, "details", total=5)
    def payload(): return json.loads(storage.read_text(1,"default",catalog_sync.CATALOG_PROGRESS_NAME))
    for complete, elapsed in [(1,4),(2,9)]:
        timer.value = elapsed
        catalog_sync.report_progress(writer,"details",total=5,completed=complete,succeeded=complete)
        assert payload()["eta_seconds"] is None and payload()["eta_confidence"] == "learning"
    timer.value = 15
    catalog_sync.report_progress(writer,"details",total=5,completed=3,succeeded=3)
    assert payload()["eta_seconds"] == 10 and payload()["eta_confidence"] == "estimated"
    before = storage.read_text(1,"default",catalog_sync.CATALOG_PROGRESS_NAME)
    timer.value = 30
    assert storage.read_text(1,"default",catalog_sync.CATALOG_PROGRESS_NAME) == before, "ETA is an observation, not a background countdown"
    catalog_sync.report_progress(writer,"images",total=2)
    assert payload()["eta_seconds"] is None
    writer({"phase":"images","total":2,"completed":1,"succeeded":1,"failed":0,"remaining":1,
            "batch_total":2,"batch_completed":1,"current_item_id":"private product body","skipped":0,"remaining_after_batch":0,
            "Cookie":"never persist this","image_body":"sensitive"})
    assert payload()["current_item_id"] == "" and "Cookie" not in payload() and "image_body" not in payload()
    writer.finish("paused")
    assert payload()["status"] == "paused" and payload()["job_id"] == 7
    path = storage.account_dir(1,"default") / catalog_sync.CATALOG_PROGRESS_NAME
    assert path.stat().st_mode & 0o777 == 0o600
    active["value"] = False
    before = path.read_bytes()
    try: writer.finish("failed")
    except RuntimeError: pass
    else: raise AssertionError("stale identity wrote progress")
    assert path.read_bytes() == before

def consumer_contract():
    db = DB(str(RUN / "consumer.db")); storage = AccountStorage(RUN / "tenants")
    uid = db.create_user("catalog-fixture-owner", "fixture-test-pass")
    account = db.ensure_default_shop_account(uid)
    storage.write_text(uid, "default", "cookies.txt", COOKIE)
    fingerprint = hashlib.sha256(COOKIE.encode()).hexdigest()[:24]
    calls, mode = [], {"value": "ready"}
    def fake_sync(cookie, **kwargs):
        if kwargs:
            assert kwargs["include_details"] is True
            if mode["value"] != "paused":
                kwargs["before_request"]()
            calls.append("details")
            if mode["value"] == "refresh_all":
                assert kwargs["refresh_all"] is True
            if mode["value"] == "selected":
                assert kwargs["item_ids"] == ["100"] and kwargs["resume_cached"] is True
            if mode["value"] == "cookie_changed":
                storage.write_text(uid, "default", "cookies.txt", "unb=900001; _m_h5_tk=new-fixture_tail")
                kwargs["before_request"]()
            if mode["value"] == "generation_changed":
                with db._lock:
                    db.con.execute("UPDATE shop_accounts SET generation=generation+1 WHERE user_id=? AND id=?", (uid, account["id"]))
                    db.con.commit()
                kwargs["before_request"]()
            if mode["value"] == "paused":
                return shop_sync.sync_shop(cookie, request_func=lambda *_: (_ for _ in ()).throw(AssertionError("paused network")), **kwargs)
            item = {**product(), "description": "真实缓存详情", "description_source": "detail", "detail_sync_status": "ready", "detail_synced_at": "2026-10-14T00:00:00+00:00"}
            if mode["value"] == "risk_partial":
                item["detail_sync_status"] = "failed"
                return {"version": 1, "account_ref": REFERENCE, "nickname": "合成消费者店", "products": [item], "product_count": 1,
                        "truncated": False, "synced_at": "2026-10-14T00:00:00+0000",
                        "catalog_sync": {"status": "partial", "ready": 0, "failed": 1, "pending": 0, "stopped_reason": "risk_control"}}
            return {"version": 1, "account_ref": REFERENCE, "nickname": "合成消费者店", "products": [item], "product_count": 1, "truncated": False, "synced_at": "2026-10-14T00:00:00+0000", "catalog_sync": {"status": "ready", "ready": 1, "failed": 0, "pending": 0, "stopped_reason": ""}}
        calls.append("ordinary")
        return {"version": 1, "account_ref": REFERENCE, "nickname": "合成消费者店", "products": [{**product(), "description": "列表短摘要"}], "product_count": 1, "truncated": False, "synced_at": "2026-10-14T00:00:00+0000"}
    consumer = JobConsumer(db, storage=storage, sync_func=fake_sync, reserve_sync_func=lambda *_: None,
                           maintenance_func=lambda: False, owner="catalog-fixture-consumer")
    payload = {"replace_cookie": False, "include_details": True, "cookie_fingerprint": fingerprint, "account_generation": int(account["generation"] or 0)}
    def queued(label, body):
        row = db.enqueue_job(uid, "shop_sync", label, account_id=account["id"], payload=body, max_attempts=1)
        assert consumer.run_once() == 1
        return db.get_job(row["id"])
    detailed = queued("enhanced", payload)
    assert detailed["status"] == "completed" and calls == ["details"]
    progress = json.loads(storage.read_text(uid, "default", "catalog_progress.json"))
    assert progress["job_id"] == detailed["id"] and progress["account_id"] == account["id"]
    assert progress["status"] == "completed" and progress["phase"] == "saving" and progress["completed"] == 1
    assert progress["cookie_fingerprint"] == fingerprint and COOKIE not in json.dumps(progress)
    before = storage.read_text(uid, "default", "shop_snapshot.json")
    ordinary = queued("ordinary", {"replace_cookie": False})
    assert ordinary["status"] == "completed" and calls[-1] == "ordinary"
    assert json.loads(storage.read_text(uid, "default", "shop_snapshot.json"))["products"][0]["description"] == "真实缓存详情"
    snapshot_before_pause = json.loads(storage.read_text(uid, "default", "shop_snapshot.json"))
    state_before_pause = storage.read_text(uid, "default", "shop_sync_state.json")
    account_before_pause = db.get_shop_account(uid, account_id=account["id"])["status"]
    storage.write_text(uid, "default", catalog_sync.CATALOG_CONTROL_NAME, json.dumps({"version":1,"paused":True}))
    mode["value"] = "paused"
    assert queued("paused", payload)["status"] == "completed"
    assert json.loads(storage.read_text(uid, "default", "shop_snapshot.json"))["products"] == snapshot_before_pause["products"]
    assert storage.read_text(uid, "default", "shop_sync_state.json") == state_before_pause
    assert db.get_shop_account(uid, account_id=account["id"])["status"] == account_before_pause
    storage.write_text(uid, "default", catalog_sync.CATALOG_CONTROL_NAME, json.dumps({"version":1,"paused":False}))
    mode["value"] = "selected"
    assert queued("selected", {**payload, "item_ids": ["100"], "resume_cached": True})["status"] == "completed"
    mode["value"] = "refresh_all"
    assert queued("full-refresh", {**payload, "refresh_all":True})["status"] == "completed"
    mode["value"] = "ready"
    call_count = len(calls)
    for index, invalid in enumerate([{**payload, "include_details": "true"}, {**payload, "include_details": 1},
                                     {**payload, "account_generation": 99}, {**payload, "account_generation": True},
                                     {**payload, "cookie_fingerprint": ""}, {**payload, "cookie_fingerprint": "f" * 24}, {**payload, "item_ids":[100]}, {**payload, "resume_cached":1}, {**payload, "refresh_all":1}]):
        row = queued(f"invalid-{index}", invalid)
        assert row["status"] == "dead_letter" and len(calls) == call_count
    old = storage.read_text(uid, "default", "shop_snapshot.json")
    mode["value"] = "cookie_changed"
    assert queued("changed-cookie", payload)["status"] == "dead_letter"
    assert storage.read_text(uid, "default", "shop_snapshot.json") == old
    storage.write_text(uid, "default", "cookies.txt", COOKIE)
    mode["value"] = "ready"
    original = db.renew_control_lease
    with patch.object(db, "renew_control_lease", side_effect=lambda key, *args, **kwargs: False if key == "shop-sync:egress" else original(key, *args, **kwargs)):
        assert queued("lost-egress", payload)["status"] == "dead_letter"
    assert storage.read_text(uid, "default", "shop_snapshot.json") == old
    mode["value"] = "generation_changed"
    assert queued("changed-generation", payload)["status"] == "dead_letter"
    assert storage.read_text(uid, "default", "shop_snapshot.json") == old
    payload["account_generation"] = int(db.get_shop_account(uid, account_id=account["id"])["generation"])
    mode["value"] = "risk_partial"
    assert queued("risk-receipt", payload)["status"] == "completed"
    assert json.loads(storage.read_text(uid, "default", "shop_sync_state.json"))["code"] == "risk_control"
    current = db.get_shop_account(uid, account_id=account["id"])
    assert current["status"] == "degraded" and current["last_error_code"] == "risk_control"
    assert json.loads(storage.read_text(uid,"default","catalog_progress.json"))["status"] == "partial"
    db.con.close()


def main():
    try:
        helper_contract(); http_contract(); incremental_contract(); image_contract(); progress_contract(); consumer_contract()
        print("catalog-sync contract: signed synthetic HTTP, serial pacing, 1000 listing cap / 20 detail batch, partial budget, fail-stop, exact cache ownership, job flags/fencing and legacy preservation passed")
    finally:
        TEMP.cleanup()


if __name__ == "__main__": main()
