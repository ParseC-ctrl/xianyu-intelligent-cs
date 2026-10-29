#!/usr/bin/env python3
"""Offline contracts: no platform requests, image downloads or live AI calls."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from catalog_sku import reconcile_sku, safe_image_url, enrich_snapshot_skus
from ai_customer_service import AIService
from material_knowledge import MaterialKnowledgeStore
from shop_sync import ShopSyncError

KNOWN = {"DEMO10001-01", "DEMO2002-02"}
URL = "https://img.alicdn.com/test-last.jpg"


def result(description, codes, status="clear", **extra):
    image = Mock(return_value={"status": status, "codes": codes})
    product = {"id": "1", "description": description, "image_urls": [URL], **extra}
    return reconcile_sku(product, KNOWN, image), image


def run():
    value, _ = result("货号：DEMO10001-01", ["DEMO10001-01"])
    assert value["sku_check"]["status"] == "matched"
    conflict, _ = result("货号：DEMO10001-01", ["DEMO2002-02"], merchant_code="DEMO2002-02")
    assert conflict["codes"] == ["DEMO10001-01"] and conflict["sku_check"]["status"] == "conflict"
    assert result("没有货号", ["DEMO2002-02"])[0]["sku_check"]["status"] == "image_only"
    assert result("无货号", ["DEMO2002-02-43"])[0]["codes"] == ["DEMO2002-02"]
    assert result("无货号", ["DEMO2002-02", "DEMO2002-02-43"])[0]["codes"] == ["DEMO2002-02"]
    assert result("无货号", ["DEMO 2002 – 02"])[0]["codes"] == ["DEMO2002-02"]
    assert result("无货号", ["UNKNOWN123-01-43"])[0]["codes"] == ["UNKNOWN123-01-43"]
    # The trailing shoe size is not part of the catalogue SKU. Each example is
    # synthetic; include letters before and after digits, and different sizes.
    fixtures = [
        ("DEMOX6938-02-42", "DEMOX6938-02"),
        ("DEMOY6105-01-40", "DEMOY6105-01"),
        ("DEMO30155A-01-43", "DEMO30155A-01"),
        ("DEMO35127-03-40", "DEMO35127-03"),
        ("DEMO3097M-02-38", "DEMO3097M-02"),
    ]
    known = {sku for _, sku in fixtures}
    for full, sku in fixtures:
        item = {"description": "模拟商品描述，款式及尺码见页面。 " + full, "image_urls": []}
        checked = reconcile_sku(item, known, Mock())
        assert checked["sku_check"]["description_code"] == sku, (full, checked)
        assert checked["codes"] == [sku]
        assert checked["sku_check"]["status"] == "description_only"
    item = {"description": "DEMOX6938-02-42", "image_urls": []}
    both = known | {"DEMOX6938-02-42"}
    assert reconcile_sku(item, both, Mock())["codes"] == ["DEMOX6938-02-42"]
    assert reconcile_sku({"description": "货号 DEMOX6938-02-42", "image_urls": []}, known, Mock())["codes"] == ["DEMOX6938-02"]
    assert reconcile_sku({"description": "DEMOX6938–02–42", "image_urls": []}, known, Mock())["codes"] == ["DEMOX6938-02"]
    assert not reconcile_sku({"description": "DEMOX6938-02-34", "image_urls": []}, known, Mock())["codes"]
    assert not reconcile_sku({"description": "UNKNOWN123-01-42", "image_urls": []}, known, Mock())["codes"]
    assert not reconcile_sku({"description": "DEMO10001-42", "image_urls": []}, {"DEMO10001"}, Mock())["codes"]
    assert reconcile_sku({"description": "货号 DEMO10001-42", "image_urls": []}, {"DEMO10001"}, Mock())["codes"] == ["DEMO10001-42"]
    assert reconcile_sku({"description": "货号 UNKNOWN123-01-42", "image_urls": []}, known, Mock())["codes"] == ["UNKNOWN123-01-42"]
    multi = reconcile_sku({"description": "DEMOX6938-02-42 和 DEMOY6105-01-40", "image_urls": []}, known, Mock())
    assert multi["sku_check"]["status"] == "ambiguous" and multi["codes"] == []
    assert reconcile_sku({"title": "DEMOX6938-02-42", "description": "模拟商品", "image_urls": []}, known, Mock())["codes"] == ["DEMOX6938-02"]
    assert result("货号：DEMO10001-01", [], "none")[0]["sku_check"]["status"] == "description_only"
    assert result("货号：UNKNOWN-123", ["DEMO2002-02"])[0]["codes"] == ["UNKNOWN-123"]
    assert result("DEMO10001-01 和 DEMO2002-02", ["DEMO10001-01"])[0]["sku_check"]["status"] == "ambiguous"
    assert result("无货号", ["DEMO10001-01", "DEMO2002-02"])[0]["codes"] == []
    assert result("无货号", [], "uncertain")[0]["sku_check"]["status"] == "ambiguous"
    failed = reconcile_sku({"description": "货号DEMO10001-01", "image_urls": [URL]}, KNOWN,
                           Mock(side_effect=RuntimeError("private provider error")))
    assert failed["codes"] == ["DEMO10001-01"] and failed["sku_check"]["error_code"] == "vision_unavailable"
    assert "private" not in str(failed)
    reader = Mock(side_effect=AssertionError("cache should avoid AI"))
    assert reconcile_sku({"description": "货号DEMO2002-02", "image_urls": [URL]}, KNOWN,
                         reader, value)["sku_check"]["status"] == "conflict"
    reader.assert_not_called()
    for unsafe in ["http://evil.test/a", "https://alicdn.com.evil.test/a", "https://127.0.0.1/a",
                   "https://user:password@img.alicdn.com/a", "https://img.alicdn.com:8000/a"]:
        assert not safe_image_url(unsafe)
    assert safe_image_url(URL)
    assert safe_image_url("http://img.alicdn.com/a") == "https://img.alicdn.com/a"
    # Selected checked code precedes contradictory native merchant fields.
    store = SimpleNamespace(product_binding=lambda _: None, _records=lambda: [],
                            lookup=Mock(return_value={"status": "matched"}), _result=Mock())
    MaterialKnowledgeStore.resolve_product(store, conflict)
    store.lookup.assert_called_once_with("DEMO10001-01", "")
    # Vision uses the documented DeepSeek image route, independent of reply model.
    ai = object.__new__(AIService)
    ai._connection_generation = Mock(return_value=1)
    ai.get_runtime_connection = Mock(return_value={"provider": "openai_chat_completions",
        "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-flash", "api_key": "fixture"})
    import io
    from PIL import Image
    photo = io.BytesIO(); Image.new("RGB", (640, 800), "white").save(photo, format="JPEG")
    ai._request_pinned = Mock(return_value=(200, photo.getvalue()))
    ai._request_json = Mock(return_value={"choices": [{"message": {"content": '{"status":"clear","codes":["DEMO10001-01"]}'}}]})
    ai._routing_session = Mock(return_value="fixture")
    ai._ensure_connection_generation = Mock()
    assert ai.read_catalog_image((1, 1, "default"), URL)["codes"] == ["DEMO10001-01"]
    args = ai._request_json.call_args.args
    assert args[2] == "deepseek-flash" and args[4]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert ai._request_pinned.call_args.kwargs["method"] == "GET"
    assert set(ai._request_pinned.call_args.args[2]) == {"Accept", "User-Agent"}
    assert "DEMO10001" not in str(args[4]["messages"])  # OCR is not primed with description SKU.
    # Exercise the real adapter: mocking _request_json used to conceal that
    # it rejected all image blocks before any provider request was sent.
    import json
    ai._request_json = AIService._request_json.__get__(ai)
    ai.requester = None
    ai._allow_local_provider = Mock(return_value=False)
    response = {"choices": [{"message": {"content": '{"status":"clear","codes":["DEMO10001-01"]}'}}]}
    ai._request_pinned = Mock(side_effect=[(200, photo.getvalue()), (200, json.dumps(response).encode())])
    assert ai.read_catalog_image((1, 1, "default"), URL)["codes"] == ["DEMO10001-01"]
    outbound = json.loads(ai._request_pinned.call_args_list[1].args[1])
    assert len(outbound["messages"][1]["content"]) == 4
    assert outbound["thinking"] == {"type": "disabled"}
    from ai_provider_adapters import build_request, ProviderAdapterError
    try:
        build_request("openai_chat_completions", "https://example.test/v1", "vision", "fixture", {"messages": outbound["messages"]})
        raise AssertionError("ordinary text path must still reject image blocks")
    except ProviderAdapterError:
        pass
    bad_messages = [
        [{"role": "system", "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/"}}]}],
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://127.0.0.1/private"}}]}],
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,invalid!"}}]}],
        [{"role": "user", "content": [outbound["messages"][1]["content"][1]] * 4}],
    ]
    for bad in bad_messages:
        try:
            build_request("openai_chat_completions", "https://example.test/v1", "vision", "fixture", {"messages": bad}, image_input=True)
            raise AssertionError("unsafe visual input accepted")
        except ProviderAdapterError:
            pass
    product = {"id": "1", "description": "货号DEMO10001-01", "image_urls": [URL], "detail_sync_status": "ready", "detail_synced_at": "new"}
    svc = SimpleNamespace(material_store=lambda *args: SimpleNamespace(_records=lambda: [{"sku": c, "style_code": ""} for c in KNOWN]),
                          read_catalog_image=Mock(return_value={"status": "clear", "codes": ["DEMO10001-01"]}))
    snap = {"products": [product], "catalog_sync": {"remaining_pending": 0}}
    paused = enrich_snapshot_skus(snap, {}, svc, (1, 1, "default"), Mock(side_effect=ShopSyncError("catalog_paused", "paused")))
    assert paused["products"][0]["sku_check_pending"] and paused["catalog_sync"]["remaining_pending"] == 1
    svc.read_catalog_image.assert_not_called()
    events = []
    unprocessed = enrich_snapshot_skus(snap, snap, svc, (1, 1, "default"), lambda: None, progress=events.append)
    assert events[0]["phase"] == "images" and events[0]["total"] == 1 and events[0]["completed"] == 0
    assert events[-1]["completed"] == events[-1]["succeeded"] == 1 and events[-1]["remaining"] == 0
    assert unprocessed["products"][0]["sku_check"]["status"] == "matched", "cached images with no check must be analysed"
    resumed = enrich_snapshot_skus(paused, paused, svc, (1, 1, "default"), lambda: None)
    assert resumed["products"][0]["sku_check"]["status"] == "matched"
    assert not resumed["products"][0].get("sku_check_pending")
    print("catalog-sku contract: priority, ambiguity, cache, isolated vision payload and pause/resume passed")


if __name__ == "__main__":
    run()
