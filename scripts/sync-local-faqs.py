#!/usr/bin/env python3
"""Synchronize an explicitly configured private FAQ source after deployment."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT/'.local/faq-sync.json'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.faq-sync-', dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2)
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate(config):
    if not isinstance(config,dict) or set(config) != {'source','origin','credentials','account'}:
        raise ValueError('invalid private sync configuration')
    parsed=urllib.parse.urlsplit(config['origin'])
    if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost'} or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment or not (1 <= (parsed.port or 80) <= 65535):
        raise ValueError('only a loopback origin is allowed')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',config['account']):
        raise ValueError('invalid account')
    for key in ('source','credentials'):
        if not Path(config[key]).is_absolute() or not Path(config[key]).is_file():
            raise ValueError('private source or credential file unavailable')
    auth=json.loads(Path(config['credentials']).read_text())
    if not isinstance(auth,dict) or set(auth)!={'username','password'} or any(not isinstance(v,str) or not v for v in auth.values()):
        raise ValueError('invalid local administrator credential format')
    data=Path(config['source']).read_bytes()
    if len(data)>1024*1024:
        raise ValueError('private source too large')
    payload=json.loads(data)
    if not isinstance(payload,dict) or set(payload)!={'faqs'}:
        raise ValueError('FAQ payload required')
    spec=importlib.util.spec_from_file_location('local_faq_update',ROOT/'scripts/update-local-faqs.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    helper.merge_faqs([],payload['faqs'])
    for row in payload['faqs']:
        if set(row)-{'id','question','answer','topics','enabled','fixed_reply'} or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',row['id']):
            raise ValueError('invalid FAQ fields')
        for key,limit in (('question',300),('answer',1500)):
            if not isinstance(row.get(key),str) or not row[key].strip() or len(row[key])>limit:
                raise ValueError('invalid FAQ text')
        if not isinstance(row.get('enabled',False),bool) or not isinstance(row.get('fixed_reply',False),bool):
            raise ValueError('invalid FAQ flags')
        topics=row.get('topics',[])
        if not isinstance(topics,list) or len(topics)>8 or any(not isinstance(x,str) or not x.strip() or len(x)>40 for x in topics):
            raise ValueError('invalid FAQ topics')
    return payload


def default_origin():
    values={}
    path=ROOT/'config/customer.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key,value=line.split('=',1);values[key.strip()]=value.strip()
    port=os.environ.get('SAAS_CUSTOMER_WEB_PORT',values.get('SAAS_CUSTOMER_WEB_PORT','4173'))
    if not port.isdigit() or not 1<=int(port)<=65535:
        raise ValueError('invalid local web port')
    return 'http://127.0.0.1:'+port


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configure',action='store_true')
    parser.add_argument('--source',type=Path)
    parser.add_argument('--credentials',type=Path,default=ROOT/'.local/windows-admin.json')
    parser.add_argument('--account',default='default')
    parser.add_argument('--origin')
    parser.add_argument('--validate',action='store_true')
    args=parser.parse_args()
    if args.configure:
        if not args.source:parser.error('--source required for initial setup')
        config={'source':str(args.source.expanduser().resolve()),'credentials':str(args.credentials.expanduser().resolve()),
                'account':args.account,'origin':args.origin or default_origin()}
        validate(config);atomic_json(CONFIG,config)
        print('Private FAQ deployment synchronization configured; no server data changed')
        return
    if not CONFIG.exists():
        if not args.validate:print('Private FAQ auto-sync is not configured; service started without FAQ changes')
        return
    config=json.loads(CONFIG.read_text());payload=validate(config)
    if args.validate:return
    # Read once and use a private local snapshot if the share changes mid-deploy.
    snapshot=ROOT/'.local/faq-sync-input.json';atomic_json(snapshot,payload)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline=time.monotonic()+120
    while True:
        try:
            with opener.open(config['origin'].rstrip('/')+'/xianyu-intelligent-cs/api/auth/capabilities',timeout=3) as response:
                if response.status==200:break
        except Exception:
            if time.monotonic()>=deadline:raise RuntimeError('local service readiness timeout')
        time.sleep(1)
    subprocess.run([sys.executable,str(ROOT/'scripts/update-local-faqs.py'),'--file',str(snapshot),
                    '--credentials',config['credentials'],'--account',config['account'],'--origin',config['origin']],check=True)
    atomic_json(ROOT/'.local/faq-sync-status.json',{'succeeded_at':time.time(),'account':config['account'],
                'source_sha256':hashlib.sha256(snapshot.read_bytes()).hexdigest()})
    print('Private FAQ deployment synchronization succeeded')


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Private FAQ synchronization failed: '+type(error).__name__+'. Existing data preserved; deployment is not fully complete.',file=sys.stderr)
        raise SystemExit(1)
