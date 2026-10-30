#!/usr/bin/env python3
"""Regression coverage for product confirmation and short buyer messages."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from material_knowledge import MaterialKnowledgeStore
from knowledge_reply import answer_material_question, buyer_size_context, listing_size_fact


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = MaterialKnowledgeStore(Path(directory).resolve() / "shop")
        faqs = [
            {"id": "origin", "question": "哪里发货", "answer": "从测试地点发货哦。", "topics": ["哪里发货"], "enabled": True, "fixed_reply": True},
            {"id": "stock", "question": "还能买吗", "answer": "页面可以下单就能购买哦。", "topics": ["这鞋不卖吗"], "enabled": True, "fixed_reply": True},
            {"id": "price", "question": "议价", "answer": "测试政策：一口价不议价。", "topics": ["最低价"], "enabled": True, "fixed_reply": True},
            {"id": "fit", "question": "尺码怎么选", "answer": "测试尺码指南。", "topics": ["选码"], "enabled": True, "fixed_reply": True},
        ]
        store.save_faqs(faqs)
        def model(category):
            return lambda _: json.dumps({"intent": category, "fields": [], "faq_id": ""})
        def ask(message, facts=None, history=None, category="business_human"):
            return answer_material_question(store, message=message, facts=facts, history=history, model_call=model(category))
        facts = {"title": "测试黑色42码休闲鞋包邮不退不换"}
        assert listing_size_fact(facts) == "42"
        result = ask("黑色 42码?", facts)
        assert result["decision"] == "reply" and "黑色" in result["reply"] and "42码" in result["reply"], result
        mismatch = ask("白色 43码?", facts)
        assert "黑色" in mismatch["reply"] and "42码" in mismatch["reply"] and "白色" not in mismatch["reply"], mismatch
        unknown = ask("黑色 42码?", {"title": "测试鞋"})
        assert "42码" not in unknown["reply"] and "黑色" not in unknown["reply"], unknown
        conflict = ask("黑色 42码?", {"title": "测试黑色42码", "description": "尺码43，白色"})
        assert "42码" not in conflict["reply"] and "黑色" not in conflict["reply"], conflict
        for question, faq_id in [("哪里发货", "origin"), ("这鞋不卖吗？", "stock"), ("50", "price"), ("50\n1要了", "price"), ("运动鞋穿42.5", "fit")]:
            result = ask(question, facts)
            assert result["decision"] == "reply" and result.get("faq_id") == faq_id, (question, result)
        for question, faq_id in [("哪里发货", "origin"), ("这鞋不卖吗？", "stock"), ("80", "price")]:
            assert ask(question, facts, category="social").get("faq_id") == faq_id
        assert "测试黑色42码休闲鞋" in ask("这双什么款式", facts)["reply"]
        assert listing_size_fact({"description": "42码已售罄"}) == ""
        memory = buyer_size_context([], "运动鞋穿42.5")[-1]
        assert memory["size"] == "42.5" and memory["shoe_type"] == "运动鞋", memory
        size_context = [{"role": "assistant", "content": "平时运动鞋穿多少码？"}]
        result = ask("42", facts, history=size_context)
        assert result.get("faq_id") == "fit", result
        assert ask("80", facts, history=size_context).get("faq_id") == "price"
        store.save_faqs([x for x in faqs if x["id"] != "origin"])
        assert ask("哪里发货", facts)["decision"] == "handoff"
        assert ask("转人工", facts, category="human")["reason_code"] == "buyer_requests_human"
        assert ask("退款并问哪里发货", facts)["decision"] == "handoff"
    print("customer intent regression: ok")


if __name__ == "__main__":
    main()
