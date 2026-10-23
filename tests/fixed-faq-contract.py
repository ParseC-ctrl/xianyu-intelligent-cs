#!/usr/bin/env python3
"""Fixed FAQ mode uses synthetic answers; no real shop policy is embedded."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'backend'))
from material_knowledge import MaterialKnowledgeStore,MaterialKnowledgeError
from knowledge_reply import answer_material_question


def main():
    with tempfile.TemporaryDirectory() as directory:
        store=MaterialKnowledgeStore(Path(directory).resolve())
        rows=[{'id':'timing','question':'发货和配送时效','topics':['下单后多久发出','多久到'], 'answer':'合成时效说明甲', 'enabled':True,'fixed_reply':True},
              {'id':'carrier','question':'能否换快递或补差价','topics':['换快递','补差价','下单发什么快递','发什么快递'], 'answer':'合成承运商说明乙','enabled':True,'fixed_reply':True}]
        empty_version=store.reply_version()
        assert store.save_faqs(rows)==store.get_faqs()
        assert store.reply_version()!=empty_version
        calls=[]
        def model(messages):
            payload=json.loads(messages[-1]['content']);calls.append(payload)
            assert 'verified_answer' not in payload,'Fixed FAQ must not call wording generation'
            current=payload.get('message') or payload.get('current_message') or ''
            identifier='carrier' if any(x in current for x in ['快递','差价','加钱']) else 'timing'
            return json.dumps({'intent':'faq','fields':[],'faq_id':identifier})
        for message,identifier in [('下单后多久发出','timing'),('多久到','timing'),('下单发什么快递','carrier'),('发什么快递','carrier'),('能不能换快递','carrier'),('我补差价呢','carrier')]:
            start=len(calls)
            result=answer_material_question(store,message=message,facts=None,history=[{'role':'assistant','content':rows[0 if identifier=='timing' else 1]['answer']}],model_call=model)
            expected=rows[0 if identifier=='timing' else 1]['answer']
            assert result['reply']==expected,(message,result)
            assert result['reply_method']=='reviewed_fixed' and result['generation_attempts']==0
            assert len(calls)-start==1
        offline=answer_material_question(store,message='换快递',facts=None)
        assert offline['reply']==rows[1]['answer']
        assert answer_material_question(store,message='下单发什么快递',facts=None)['reply']==rows[1]['answer']
        version=store.reply_version();rows[0]['fixed_reply']=False;store.save_faqs(rows)
        assert store.reply_version()!=version
        bad={**rows[1],'fixed_reply':'true'}
        try:store.save_faqs([bad])
        except MaterialKnowledgeError:pass
        else:raise AssertionError('Non-boolean fixed_reply accepted')
        spec=importlib.util.spec_from_file_location('faq_update',root/'scripts/update-local-faqs.py')
        helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
        assert helper.merge_faqs([{'id':'keep','answer':'unchanged'},{'id':'timing','answer':'old'}],rows)==[{'id':'keep','answer':'unchanged'},rows[0],rows[1]]
        # Check mode must not update FAQs or print answers/credentials.
        import contextlib,io
        from unittest.mock import patch
        credentials=Path(directory)/'credentials.json'
        credentials.write_text(json.dumps({'username':'fixture','password':'private-fixture-password'}))
        calls=[]
        def open_local(req,timeout):
            path=req.full_url.split('/xianyu-intelligent-cs',1)[1]
            calls.append((req.get_method(),path))
            values={'/api/auth/login':{},'/api/auth/logout':{},'/api/bot/materials/faqs':{'faqs':rows},
                    '/api/version/public':{'version':'fixture'},'/api/bot/conversations?limit=1':{'needs_human_total':1},
                    '/api/bot/materials/preview':{'decision':'reply','reply':rows[1]['answer'],'faq_id':'carrier','sent':False,'preview_mode':'offline_rules'}}
            return io.BytesIO(json.dumps(values[path]).encode())
        output=io.StringIO()
        with patch.object(sys,'argv',['update-local-faqs.py','--check','--question','下单发什么快递','--credentials',str(credentials)]), patch.object(helper.urllib.request,'build_opener') as opener, contextlib.redirect_stdout(output):
            opener.return_value.open.side_effect=open_local
            helper.main()
        report=json.loads(output.getvalue())
        assert report['preview']['sent'] is False and report['conversations_pending_owner']==1
        assert not any(method=='PUT' for method,path in calls)
        assert rows[1]['answer'] not in output.getvalue() and 'private-fixture-password' not in output.getvalue()
        try:helper.merge_faqs([],rows+rows)
        except ValueError:pass
        else:raise AssertionError('Duplicate update IDs accepted')
    print('fixed-faq contract: exact answers, one classification, persistence, version fencing and private merge passed')


if __name__=='__main__':main()
