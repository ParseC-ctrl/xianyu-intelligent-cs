#!/usr/bin/env python3
"""Conversation replay contracts: repeated wording must not force policy changes."""
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from knowledge_reply import answer_material_question
from reply_style import repetitive, grounded_fallback, normalized, repeated_price_phrase


class Store:
    version = 'a' * 64
    def reply_version(self): return self.version
    def get_faqs(self):
        return [{'id': 'price', 'question': '可以议价吗', 'topics': ['能刀吗'],
                 'answer': '商品按标价出售，暂不议价。', 'enabled': True}]


def classify():
    return json.dumps({'intent': 'faq', 'fields': [], 'faq_id': 'price'})


class ReplyStyleContracts(unittest.TestCase):
    def ask(self, callback, history):
        return answer_material_question(Store(), message='求求你，便宜一点', facts=None,
                                        history=history, model_call=callback)

    def test_duplicate_is_rewritten_once_and_answer_is_preserved(self):
        calls = []
        def model(messages):
            data = json.loads(messages[-1]['content']); calls.append(data)
            if 'verified_answer' not in data: return classify()
            return json.dumps({'reply': '这双不刀，按标价出。' if not data['revision'] else '想便宜点我明白，不过价格没法再少啦。'}, ensure_ascii=False)
        result = self.ask(model, [{'role': 'assistant', 'content': '这双不刀，按标价出。'}])
        self.assertEqual(result['generation_attempts'], 2)
        self.assertEqual(result['reply_method'], 'model_grounded')
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[-1]['revision'])
        self.assertFalse(repetitive(result['reply'], ['这双不刀，按标价出。']))

    def test_persistent_duplicate_uses_different_grounded_fallback(self):
        def model(messages):
            return json.dumps({'reply': '这双不刀，按标价出。'}, ensure_ascii=False) if 'verified_answer' in json.loads(messages[-1]['content']) else classify()
        result = self.ask(model, [{'role': 'assistant', 'content': '这双不刀，按标价出。'}])
        self.assertEqual(result['generation_attempts'], 2)
        self.assertEqual(result['generation_fallback_reason'], 'recent_duplicate')
        self.assertNotEqual(result['reply'], '这双不刀，按标价出。')

    def test_outage_does_not_retry_or_repeat_one_fallback(self):
        history = []
        answers = []
        for _ in range(8):
            count = []
            def model(messages):
                if 'verified_answer' not in json.loads(messages[-1]['content']): return classify()
                count.append(1)
                raise TimeoutError('synthetic provider outage')
            result = self.ask(model, history)
            self.assertEqual(len(count), 1)
            self.assertEqual(result['reply_method'], 'verified_fallback')
            answers.append(normalized(result['reply']))
            history.extend([{'role': 'user', 'content': '少点'}, {'role': 'assistant', 'content': result['reply']}])
        self.assertEqual(len(set(answers)), 8)

    def test_negotiation_cannot_be_reversed_for_variation(self):
        for offer in ['不能议价，但可以给你优惠。', '按标价出，便宜点也可以。', '给你打折，价格不变。']:
            def model(messages):
                return json.dumps({'reply': offer}, ensure_ascii=False) if 'verified_answer' in json.loads(messages[-1]['content']) else classify()
            result = self.ask(model, [{'role': 'assistant', 'content': '这双不刀。'}])
            self.assertEqual(result['reply_method'], 'verified_fallback', offer)
            self.assertNotEqual(result['reply'], offer)

    def test_price_reply_does_not_append_repetitive_purchase_invitation(self):
        def model(messages):
            data = json.loads(messages[-1]['content'])
            if 'verified_answer' not in data: return classify()
            return json.dumps({'reply': '不议价，合适直接拍。' if not data['revision'] else '这双价格不调整。'}, ensure_ascii=False)
        result = self.ask(model, [])
        self.assertEqual(result['generation_attempts'], 2)
        self.assertEqual(result['reply_method'], 'model_grounded')
        self.assertNotIn('拍', result['reply'])

    def test_repeated_bargaining_must_not_scold_the_buyer(self):
        for bad in ['再求也是一样的，谢谢理解。', '都说了不议价，别问了。']:
            def model(messages):
                data = json.loads(messages[-1]['content'])
                if 'verified_answer' not in data: return classify()
                return json.dumps({'reply': bad if not data['revision'] else '这双价格不调整。'}, ensure_ascii=False)
            result = self.ask(model, [])
            self.assertEqual(result['generation_attempts'], 2)
            self.assertEqual(result['reply'], '这双价格不调整。')

    def test_same_price_ending_is_rewritten_even_when_whole_sentence_differs(self):
        old = '价格没有余地了，还是按页面标价来。'
        bad = '确实没法便宜，就按页面标价来。'
        self.assertTrue(repeated_price_phrase(bad, [old]))
        def model(messages):
            data = json.loads(messages[-1]['content'])
            if 'verified_answer' not in data: return classify()
            return json.dumps({'reply': bad if not data['revision'] else '这双不刀的。'}, ensure_ascii=False)
        result = self.ask(model, [{'role': 'assistant', 'content': old}])
        self.assertEqual(result['generation_attempts'], 2)
        self.assertEqual(result['reply'], '这双不刀的。')

    def test_reluctant_ack_is_warm_and_does_not_reopen_negotiation(self):
        for message in ['那好吧', '好吧', '行吧']:
            def model(messages):
                data = json.loads(messages[-1]['content'])
                if 'verified_answer' not in data:
                    return json.dumps({'intent': 'ack', 'fields': [], 'faq_id': ''})
                return json.dumps({'reply': '好，那先这样' if not data['revision'] else '好呀，你慢慢考虑～'}, ensure_ascii=False)
            result = answer_material_question(Store(), message=message, facts=None,
                                             history=[{'role': 'assistant', 'content': '这双不议价哦。'}], model_call=model)
            self.assertEqual(result['intent'], 'ack')
            self.assertEqual(result['generation_attempts'], 2)
            self.assertEqual(result['reply'], '好呀，你慢慢考虑～')

    def test_ack_outage_remains_warm_and_varies(self):
        history = []
        replies = []
        def model(messages):
            if 'verified_answer' not in json.loads(messages[-1]['content']):
                return json.dumps({'intent': 'ack', 'fields': [], 'faq_id': ''})
            raise TimeoutError('synthetic')
        for _ in range(3):
            result = answer_material_question(Store(), message='那好吧', facts=None, history=history, model_call=model)
            replies.append(result['reply'])
            history.append({'role': 'assistant', 'content': result['reply']})
        self.assertEqual(len(set(replies)), 3)
        self.assertTrue(all('不客气' not in reply and '议价' not in reply for reply in replies))

    def test_handoff_vocabulary_hidden_without_changing_material(self):
        from reply_style import buyer_facing_text
        from ai_reply_engine import _parse_model_decision
        self.assertEqual(buyer_facing_text('鞋面是人工革。'), '鞋面是人工革。')
        for text in ['需要人工客服确认。', '特殊情况由人工确认。', '先转人工帮你看。']:
            self.assertNotIn('人工', buyer_facing_text(text))
            self.assertNotIn('人工', _parse_model_decision(json.dumps({'decision': 'reply', 'reply': text, 'reason_code': 'test'}))['reply'])

    def test_version_fence_also_applies_to_failed_generation(self):
        store = Store()
        def model(messages):
            if 'verified_answer' not in json.loads(messages[-1]['content']): return classify()
            store.version = 'b' * 64
            raise TimeoutError('synthetic')
        result = answer_material_question(store, message='便宜点', facts=None, model_call=model)
        self.assertEqual(result['reason_code'], 'knowledge_changed')


if __name__ == '__main__': unittest.main()
