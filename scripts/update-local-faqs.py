#!/usr/bin/env python3
"""Merge reviewed local FAQ updates into an existing loopback deployment."""
import argparse
import datetime
import http.cookiejar
import json
import os
from pathlib import Path
import urllib.parse
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


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='http://127.0.0.1:4173')
    parser.add_argument('--file', type=Path, required=True)
    parser.add_argument('--credentials', type=Path, default=root/'.local/windows-admin.json')
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.origin)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        parser.error('origin must be a local loopback HTTP origin')
    updates = json.loads(args.file.read_text(encoding='utf-8'))['faqs']
    auth = json.loads(args.credentials.read_text(encoding='utf-8'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
    def request(path, method='GET', payload=None):
        req = urllib.request.Request(args.origin.rstrip('/')+'/xianyu-intelligent-cs'+path,
            data=json.dumps(payload).encode() if payload is not None else None, method=method,
            headers={'Origin':args.origin.rstrip('/'), 'X-SaaS-Browser-Intent':'browser-write', 'Content-Type':'application/json'})
        with opener.open(req, timeout=30) as response:
            return json.loads(response.read())
    logged_in = False
    try:
        request('/api/auth/login', 'POST', auth)
        logged_in = True
        before = request('/api/bot/materials/faqs')['faqs']
        merged = merge_faqs(before, updates)
        private = root/'.local'; private.mkdir(mode=0o700, exist_ok=True)
        backup = private/('faq-backup-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json')
        fd = os.open(backup, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'faqs':before},stream,ensure_ascii=False)
        if request('/api/bot/materials/faqs')['faqs'] != before:
            raise RuntimeError('FAQ changed concurrently; no update sent')
        request('/api/bot/materials/faqs', 'PUT', {'faqs':merged})
        saved = request('/api/bot/materials/faqs')['faqs']
        actual = {row['id']:row for row in saved}
        for row in updates:
            if any(actual.get(row['id'],{}).get(key, False if key == 'fixed_reply' else None) != row[key] for key in ('answer','enabled','fixed_reply') if key in row):
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
    except Exception as error:
        raise SystemExit('Local FAQ update stopped: '+type(error).__name__+'. Preserve the backup and inspect locally; no credentials or response bodies are printed.')
