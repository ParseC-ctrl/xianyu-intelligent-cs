#!/usr/bin/env python3
"""Merge reviewed local FAQ updates into an existing loopback deployment."""
import argparse
import datetime
import http.cookiejar
import json
import os
import re
from pathlib import Path
import urllib.parse
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def merge_faqs(current, updates):
    if not isinstance(current, list) or not isinstance(updates, list) or not updates:
        raise ValueError('FAQ lists required')
    ids = [row.get('id') for row in updates if isinstance(row, dict)]
    if len(ids) != len(updates) or any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise ValueError('Unique FAQ IDs required')
    pending = {row['id']: row for row in updates}
    result = [pending.pop(row['id'], row) for row in current]
    result.extend(pending.values())
    if len(result) > 50:
        raise ValueError('FAQ limit exceeded')
    return result


def canonical_faqs(rows):
    return [{**row, 'question':row['question'].strip(), 'answer':row['answer'].strip(),
             'topics':sorted(set(x.strip() for x in row.get('topics',[]))),
             'enabled':row.get('enabled',False), 'fixed_reply':row.get('fixed_reply',False)} for row in rows]


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='http://127.0.0.1:4173')
    parser.add_argument('--file', type=Path)
    parser.add_argument('--check', action='store_true', help='Read-only local FAQ/preview diagnosis; never sends buyer messages')
    parser.add_argument('--question', default='')
    parser.add_argument('--model', action='store_true', help='Opt-in model preview in check mode; may consume model tokens')
    parser.add_argument('--account', default='default', help='Same shop account key as the workbench')
    parser.add_argument('--credentials', type=Path, default=root/'.local/windows-admin.json')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', args.account):
        parser.error('invalid shop account key')
    parsed = urllib.parse.urlsplit(args.origin)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        parser.error('origin must be a local loopback HTTP origin')
    if not args.check and args.file is None:
        parser.error('--file is required for updates')
    if args.model and not args.check:
        parser.error('--model requires --check')
    updates = json.loads(args.file.read_text(encoding='utf-8'))['faqs'] if args.file else []
    auth = json.loads(args.credentials.read_text(encoding='utf-8'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
    def request(path, method='GET', payload=None):
        req = urllib.request.Request(args.origin.rstrip('/')+'/xianyu-intelligent-cs'+path,
            data=json.dumps(payload).encode() if payload is not None else None, method=method,
            headers={'Origin':args.origin.rstrip('/'), 'X-SaaS-Browser-Intent':'browser-write', 'Content-Type':'application/json', 'X-Shop-Account':args.account})
        with opener.open(req, timeout=30) as response:
            return json.loads(response.read())
    logged_in = False
    try:
        request('/api/auth/login', 'POST', auth)
        logged_in = True
        before = request('/api/bot/materials/faqs')['faqs']
        if args.check:
            expected = {row['id']:row for row in updates}
            report = {'account_key':args.account, 'service':request('/api/version/public'), 'faq_status':[
                {'id':row['id'], 'enabled':row.get('enabled') is True,
                 'fixed_reply':row.get('fixed_reply') is True,
                 'question_is_exact_alias':args.question == row.get('question') or args.question in row.get('topics', []),
                 'matches_local_update':canonical_faqs([row]) == canonical_faqs([expected[row['id']]]) if row['id'] in expected else None}
                for row in before], 'expected_faq_ids_missing':sorted(set(expected)-{row['id'] for row in before})}
            conversations = request('/api/bot/conversations?limit=1')
            report['conversations_pending_owner'] = conversations.get('needs_human_total')
            if args.question:
                preview = request('/api/bot/materials/preview','POST',{'message':args.question,'use_model':args.model})
                report['preview'] = {k:preview.get(k) for k in ('decision','reason_code','intent','intent_method','faq_id','reply_method','preview_mode','sent')}
                if preview.get('faq_id') in expected:
                    report['preview']['matches_expected_answer'] = preview.get('reply') == expected[preview['faq_id']]['answer']
            print(json.dumps(report,ensure_ascii=False,indent=2))
            return
        merged = merge_faqs(before, updates)
        if canonical_faqs(before) == canonical_faqs(merged):
            print('Local FAQ configuration already matches; no write or backup needed')
            return
        private = root/'.local'; private.mkdir(mode=0o700, exist_ok=True)
        backup = private/('faq-backup-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json')
        fd = os.open(backup, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'faqs':before},stream,ensure_ascii=False)
        request('/api/bot/materials/faqs/merge', 'POST', {'faqs':updates})
        saved = request('/api/bot/materials/faqs')['faqs']
        actual = {row['id']:row for row in saved}
        for row in updates:
            if row['id'] not in actual or canonical_faqs([actual[row['id']]]) != canonical_faqs([row]):
                raise RuntimeError('FAQ verification failed; preserve backup and inspect locally')
        print('Local FAQ updates verified:',len(updates))
        print('Previous FAQ backup:',backup)
        print('No Xianyu, product sync or model request triggered')
    finally:
        if logged_in:
            try:request('/api/auth/logout','POST')
            except Exception:pass


if __name__ == '__main__':
    try: main()
    except urllib.error.HTTPError as error:
        raise SystemExit('Local FAQ request failed: HTTP '+str(error.code)+'. No response body or credentials printed.')
    except Exception as error:
        raise SystemExit('Local FAQ update stopped: '+type(error).__name__+'. Preserve the backup and inspect locally; no credentials or response bodies are printed.')
