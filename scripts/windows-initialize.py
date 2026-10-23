#!/usr/bin/env python3
"""Initialize a fresh local deployment without contacting Xianyu or AI providers."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import urllib.error
import urllib.parse
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default='http://127.0.0.1:4173')
    parser.add_argument('--seed-knowledge', action='store_true', help='Import packaged Excel/FAQs only when the corresponding local store is empty')
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.origin)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.username or parsed.password or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        parser.error('origin must be a local loopback HTTP origin')
    origin = args.origin.rstrip('/')
    root = Path(__file__).resolve().parents[1]
    private = root / '.local'; private.mkdir(mode=0o700, exist_ok=True)
    credential_file = private / 'windows-admin.json'
    if credential_file.exists():
        auth = json.loads(credential_file.read_text(encoding='utf-8'))
    else:
        auth = {'username': 'customer-admin', 'password': secrets.token_urlsafe(24)}
        descriptor = os.open(credential_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(auth, stream)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
    def request(path, method='GET', payload=None, binary=None):
        data = binary if binary is not None else json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(origin + '/xianyu-intelligent-cs' + path, data=data, method=method,
            headers={'Origin': origin, 'X-SaaS-Browser-Intent': 'browser-write',
                     'Content-Type': 'application/octet-stream' if binary is not None else 'application/json'})
        with opener.open(req, timeout=60) as response:
            return json.loads(response.read())
    logged_in = False
    try:
        try:
            request('/api/auth/login', 'POST', auth)
        except urllib.error.HTTPError as error:
            if error.code != 401 or not request('/api/auth/capabilities').get('first_registration_available'):
                raise RuntimeError('Local login failed; preserve credentials/data and use the existing administrator login') from None
            request('/api/auth/register', 'POST', auth)
            request('/api/auth/login', 'POST', auth)
        logged_in = True
        print('Local administrator ready. Credentials are stored privately at: ' + str(credential_file))
        if args.seed_knowledge:
            assets = root / 'deployment-assets'
            status = request('/api/bot/materials/status')
            if not status.get('record_count'):
                workbook = assets / '材质信息表.xlsx'
                result = request('/api/bot/materials/import?filename=' + urllib.parse.quote(workbook.name), 'POST', binary=workbook.read_bytes())
                print('Knowledge imported; SKU count: ' + str(result.get('sku_count', 'see UI')))
            else:
                print('Existing material knowledge preserved')
            faqs = request('/api/bot/materials/faqs')
            if not faqs.get('faqs'):
                seed = json.loads((assets / 'customer-faqs.json').read_text(encoding='utf-8'))
                result = request('/api/bot/materials/faqs', 'PUT', seed)
                print('FAQ entries imported: ' + str(len(result.get('faqs', []))))
            else:
                print('Existing FAQ entries preserved')
        print('No Xianyu login, product sync, worker start or model request was triggered')
    finally:
        if logged_in:
            try: request('/api/auth/logout', 'POST')
            except Exception: pass

if __name__ == '__main__':
    try: main()
    except Exception as error:
        # Do not print HTTP bodies, headers, passwords, or cookie jars.
        raise SystemExit('Initialization stopped: ' + type(error).__name__ + '. Inspect the local service and preserve .local/windows-admin.json; do not reset the data volume.')
