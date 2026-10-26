"""Bounded conversation style checks. No product facts or policies are generated here."""
from __future__ import annotations

import difflib
import re
import unicodedata


def recent_replies(history):
    return [str(x.get('content') or '')[:1200] for x in (history or [])[-24:]
            if isinstance(x, dict) and x.get('role') == 'assistant' and x.get('content')][-8:]


def normalized(text):
    text = unicodedata.normalize('NFKC', str(text)).lower()
    return re.sub(r'[\W_]+', '', text)


def repetition_score(text, previous):
    left, right = normalized(text), normalized(previous)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    # Short factual answers share unavoidable words; only reject exact repeats.
    if min(len(left), len(right)) < 12:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def repetitive(text, previous):
    return any(repetition_score(text, old) >= .86 for old in previous[-6:])


def repeated_price_phrase(text, previous):
    current = normalized(text)
    # Fixed policy may share words, but not the same six-character ending
    # on every turn (e.g. repeatedly appending the same price sentence).
    return any(difflib.SequenceMatcher(None, current, normalized(old)).find_longest_match().size >= 6
               for old in previous[-4:] if old)


def no_bargaining(text):
    return bool(re.search(r'不议价|暂不议价|不刀|谢绝议价|不接受.{0,6}报价|一口价', text))


def explicit_policy_price(text):
    prices = set(re.findall(r"(?<![\d.])\d+(?:\.\d{1,2})?\s*元", text))
    return next(iter(prices)) if len(prices) == 1 else ""


def buyer_facing_text(text):
    """Hide internal handoff vocabulary without changing material names (人工革)."""
    for old, new in (("这个问题需要人工客服确认，已加入待人工处理。", "我看看哈"),
                     ("已加入待人工处理", "我看看哈"),
                     ("转接人工客服", "请店主接着聊"), ("转人工客服", "请店主接着聊"),
                     ("转人工", "请店主接着聊"), ("人工客服", "店主"),
                     ("人工接管", "店主接着处理"), ("人工处理", "店主确认"),
                     ("人工确认", "店主确认"), ("人工核实", "店主核实")):
        text = text.replace(old, new)
    return text


def acknowledgement_choices(message):
    if re.search(r'那好|好吧|行吧|考虑|再看看|想想', message):
        return ['好呀，你慢慢考虑～', '嗯嗯，慢慢看，不着急～', '好的呀，你再看看～', '嗯呐，你慢慢考虑呀。']
    if re.search(r'谢|感谢|thanks|thank you', message, re.I):
        return ['不客气呀～', '不用客气～', '不客气的呀。', '嘿嘿，不客气～']
    return ['好呀～', '嗯嗯，好哒～', '好的呀～', '嗯呐～']


def grounded_fallback(result, history, message=""):
    """Only vary known conversational replies and an explicit no-bargaining policy."""
    source = result['reply']
    if not history and result.get("reason_code") != "acknowledged" and not (result.get("reason_code") == "faq_answered" and no_bargaining(source)):
        return source
    choices = []
    if result.get('reason_code') == 'faq_answered' and no_bargaining(source):
        choices = [
            '这双不刀，按标价出。',
            '价格就按页面的，没法再少啦。',
            '确实不能再便宜了，你再考虑看看。',
            '这双价格固定，没有议价空间。',
            '想便宜点的意思我明白，不过这双不降价。',
            '还是原来的价格，没办法再让了。',
            '小刀也不接的，价格就按标价。',
            '这双没有优惠，喜欢的话你再考虑下。',
            '价格这边不调整，就按现在的标价。',
            '确实让不了价，其他商品问题可以接着问。',
        ]
        price = explicit_policy_price(source)
        if price:
            choices = [f'这双{price}一口价哈。', f'就是{price}啦，不能再少了哈。',
                       f'还是{price}哈，小刀也不接啦。', f'价格定在{price}，不再往下让啦。',
                       f'真没法再少啦，就{price}哈。', f'{price}是定好的价格，没法再优惠啦。']
        elif re.search(r"\d", source):
            return source  # Ambiguous numeric policies must never be paraphrased blindly.
    elif result.get('reason_code') == 'acknowledged':
        choices = acknowledgement_choices(message or next((str(x.get("content") or "") for x in reversed(history or []) if x.get("role") == "user"), ""))
    elif result.get('reason_code') == 'greeting_answered':
        choices = ['在的，你说。', '你好呀，想问什么？', '在呢，接着聊。']
    if not choices:
        return source
    previous = recent_replies(history)
    unused = [x for x in choices if normalized(x) not in {normalized(old) for old in previous}]
    candidates = unused or choices
    if result.get('reason_code') == 'faq_answered' and no_bargaining(source):
        fresh_phrases = [x for x in candidates if not repeated_price_phrase(x, previous)]
        candidates = fresh_phrases or candidates
    return min(candidates, key=lambda x: max((repetition_score(x, old) for old in previous[-6:]), default=0))
