#!/usr/bin/env python3
"""Private deployment sync contracts, synthetic fixtures and local mocks only."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from material_knowledge import MaterialKnowledgeStore


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def main():
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory).resolve(); (root/'scripts').mkdir()
        shutil.copy(ROOT/'scripts/update-local-faqs.py',root/'scripts/update-local-faqs.py')
        sync=load('faq_deploy_sync',ROOT/'scripts/sync-local-faqs.py')
        helper=load('faq_deploy_update',ROOT/'scripts/update-local-faqs.py')
        fixture={'id':'managed','question':'Synthetic question','answer':'Synthetic answer A','enabled':True,'fixed_reply':True,'topics':['synthetic']}
        source=root/'private-share.json';source.write_text(json.dumps({'faqs':[fixture]}))
        credentials=root/'credentials.json';credentials.write_text(json.dumps({'username':'fixture','password':'private-fixture'}))
        sync.ROOT=root;sync.CONFIG=root/'.local/faq-sync.json'
        with patch.object(sys,'argv',['sync','--configure','--source',str(source),'--credentials',str(credentials)]):sync.main()
        assert json.loads(sync.CONFIG.read_text())['source']==str(source)
        assert sync.CONFIG.stat().st_mode & 0o077 == 0
        class Ready(io.BytesIO):status=200
        with patch.object(sync.urllib.request,'build_opener') as opener, patch.object(sync.subprocess,'run') as run:
            opener.return_value.open.side_effect=lambda *a,**k:Ready(b'{}')
            for answer in ['Synthetic answer A','Synthetic answer B']:
                fixture['answer']=answer;source.write_text(json.dumps({'faqs':[fixture]}))
                with patch.object(sys,'argv',['sync']):sync.main()
                args=run.call_args.args[0]
                snapshot=Path(args[args.index('--file')+1]);assert json.loads(snapshot.read_text())['faqs'][0]['answer']==answer
                assert args[args.index('--account')+1]=='default'
            assert run.call_count==2
        status=json.loads((root/'.local/faq-sync-status.json').read_text())
        assert 'source_sha256' in status and fixture['answer'] not in json.dumps(status)
        source.unlink()
        with patch.object(sys,'argv',['sync','--validate']),patch.object(sync.subprocess,'run') as run:
            try:sync.main()
            except ValueError:pass
            else:raise AssertionError('Unavailable private share silently accepted')
            run.assert_not_called()
        source.write_text(json.dumps({'faqs':[fixture]}))
        store=MaterialKnowledgeStore(root/'account')
        keep={**fixture,'id':'keep','answer':'Keep this unrelated FAQ'}
        store.save_faqs([keep,fixture]);version=store.reply_version()
        assert store.merge_faqs([fixture])==store.get_faqs() and store.reply_version()==version
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda key:store.merge_faqs([{**fixture,'id':key}]),['parallel-a','parallel-b']))
        assert {x['id'] for x in store.get_faqs()}=={'keep','managed','parallel-a','parallel-b'}
        requests=[]
        def serve(req,timeout):
            path=req.full_url.split('/xianyu-intelligent-cs',1)[1];requests.append((req.get_method(),path))
            if path=='/api/bot/materials/faqs/merge':result={'faqs':store.merge_faqs(json.loads(req.data)['faqs'])}
            elif path=='/api/bot/materials/faqs':result={'faqs':store.get_faqs()}
            else:result={}
            return io.BytesIO(json.dumps(result).encode())
        def apply():
            with patch.object(helper,'__file__',str(root/'scripts/update-local-faqs.py')),patch.object(sys,'argv',['update','--file',str(source),'--credentials',str(credentials)]),patch.object(helper.urllib.request,'build_opener') as opener,contextlib.redirect_stdout(io.StringIO()):
                opener.return_value.open.side_effect=serve;helper.main()
        apply();assert not any(path.endswith('/merge') for method,path in requests)
        fixture['answer']='Synthetic answer C';source.write_text(json.dumps({'faqs':[fixture]}));requests.clear();apply()
        assert ('POST','/api/bot/materials/faqs/merge') in requests and not any(method=='PUT' for method,path in requests)
        assert next(x for x in store.get_faqs() if x['id']=='keep')['answer']==keep['answer']
        assert len(list((root/'.local').glob('faq-backup-*.json')))==1
        requests.clear();apply();assert not any(path.endswith('/merge') for method,path in requests)
        assert len(list((root/'.local').glob('faq-backup-*.json')))==1
        # Verify actual shell deployment order, without a real Docker engine.
        deploy=root/'deploy';(deploy/'scripts').mkdir(parents=True);(deploy/'config').mkdir();(deploy/'bin').mkdir()
        shutil.copy(ROOT/'scripts/customer.sh',deploy/'scripts/customer.sh')
        (deploy/'config/customer.env').write_text('SAAS_CUSTOMER_WEB_PORT=4173\n')
        trace=deploy/'trace'
        (deploy/'scripts/sync-local-faqs.py').write_text('import os,sys\nfrom pathlib import Path\np=Path(os.environ["TRACE"])\nwith p.open("a") as f:f.write("validate\\n" if "--validate" in sys.argv else "sync\\n")\nif "--validate" in sys.argv and os.getenv("FAIL_PREFLIGHT"):sys.exit(1)\n')
        docker=deploy/'bin/docker';docker.write_text('#!'+sys.executable+'\nimport os,sys\nfrom pathlib import Path\nif "up" in sys.argv:\n with Path(os.environ["TRACE"]).open("a") as f:f.write("up\\n")\n');docker.chmod(0o755)
        import os
        env={**os.environ,'PATH':str(deploy/'bin')+os.pathsep+os.environ['PATH'],'TRACE':str(trace)}
        subprocess.run(['bash','scripts/customer.sh','start'],cwd=deploy,env=env,check=True)
        assert trace.read_text().splitlines()==['validate','up','sync']
        trace.unlink()
        failed=subprocess.run(['bash','scripts/customer.sh','start'],cwd=deploy,env={**env,'FAIL_PREFLIGHT':'1'})
        assert failed.returncode!=0 and trace.read_text().splitlines()==['validate']
    print('FAQ deployment sync: latest private source, account scope, idempotency, backups, concurrent preservation and startup order passed')


if __name__=='__main__':main()
