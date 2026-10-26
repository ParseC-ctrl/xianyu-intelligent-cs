#!/usr/bin/env python3
"""Execute embedded launcher shell code with fake Docker; never start real services."""
import base64
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1]
SOURCE=(ROOT/'Start-XianyuIntelligentCs.ps1').read_text()


def main():
    engine=re.search(r"\$EngineScript = @'\n(.*?)\n'@",SOURCE,re.S).group(1)
    service=re.search(r"\$ServiceScript = @'\n(.*?)\n'@",SOURCE,re.S).group(1)
    assert '.Replace("`r`n", "`n")' in SOURCE
    assert '-NoProfile' not in service and 'git pull' not in service
    for body in (engine,service):subprocess.run(['bash','-n'],input=body.encode(),check=True)
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory).resolve(); binary=root/'bin';binary.mkdir();trace=root/'trace';ready=root/'ready'
        executable='#!'+sys.executable+'\n'
        def command(name,body):
            p=binary/name;p.write_text(executable+body);p.chmod(0o755)
        command('docker','''import os,sys
from pathlib import Path
args=sys.argv[1:]
with Path(os.environ['TRACE']).open('a') as f:f.write('docker '+repr(args)+'\\n')
if args[0]=='info':sys.exit(0 if Path(os.environ['READY']).exists() else 1)
if args[0]=='ps':
 if os.getenv('PROJECT'):print('fixture-container')
elif args[0]=='inspect':print(os.environ['PROJECT'])
''')
        command('ps',"import os\nprint(os.getenv('PID1','systemd'))\n")
        command('systemctl',"import os\nfrom pathlib import Path\nPath(os.environ['READY']).touch()\nwith Path(os.environ['TRACE']).open('a') as f:f.write('engine-start\\n')\n")
        env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],'TRACE':str(trace),'READY':str(ready),'HOME':str(root)}
        subprocess.run(['bash'],input=engine.encode(),env=env,check=True,capture_output=True)
        assert trace.read_text().count('engine-start')==1
        subprocess.run(['bash'],input=engine.encode(),env=env,check=True,capture_output=True)
        assert trace.read_text().count('engine-start')==1
        command('service',"import os\nfrom pathlib import Path\nPath(os.environ['READY']).touch()\nwith Path(os.environ['TRACE']).open('a') as f:f.write('service-start\\n')\n")
        init=root/'init-docker';init.touch();init.chmod(0o755);ready.unlink()
        subprocess.run(['bash'],input=engine.replace('/etc/init.d/docker',str(init)).encode(),env={**env,'PID1':'init'},check=True,capture_output=True)
        assert 'service-start' in trace.read_text()
        command('pgrep','raise SystemExit(1)\n')
        command('dockerd',"import os\nfrom pathlib import Path\nPath(os.environ['READY']).touch()\n")
        ready.unlink()
        manual=engine.replace('/etc/init.d/docker',str(root/'missing-init')).replace('/var/log/docker-wsl.log',str(root/'docker-wsl.log'))
        subprocess.run(['bash'],input=manual.encode(),env={**env,'PID1':'init'},check=True,capture_output=True)
        assert ready.exists()
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                assert self.path=='/xianyu-intelligent-cs/api/auth/capabilities'
                self.send_response(200);self.end_headers();self.wfile.write(b'{}')
            def log_message(self,*args):pass
        server=http.server.HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            project=root/"project with ' quote";(project/'scripts').mkdir(parents=True);(project/'config').mkdir()
            (project/'compose.customer.yml').write_text('name: synthetic\n')
            (project/'config/customer.env').write_text('SAAS_CUSTOMER_WEB_PORT='+str(server.server_port)+'\n')
            (project/'scripts/customer.sh').write_text('case "$1" in\nstart|run) printf "app:%s\\n" "$1" >> "$TRACE";;\nstatus) :;;\nesac\n')
            def run(path,extra=None):
                prefix="project_encoded='"+base64.b64encode(path.encode()).decode()+"'\naction_encoded='cnVu'\n"
                return subprocess.run(['bash'],input=(prefix+service).encode(),env={**env,**(extra or {})},capture_output=True,text=False)
            result=run(str(project));assert result.returncode==0,result.stderr.decode()
            assert '__XIANYU_URL__=http://127.0.0.1:'+str(server.server_port) in result.stdout.decode()
            assert 'app:run' in trace.read_text()
            result=run('',{'PROJECT':str(project)});assert result.returncode==0,result.stderr.decode()
            result=run('/missing-fixture');assert result.returncode!=0
            assert not (root/'missing-fixture').exists()
            for name in ('xianyu-intelligent-cs','xianyu-intelligent-cs-public'):
                candidate=root/'apps'/name;(candidate/'scripts').mkdir(parents=True);(candidate/'config').mkdir()
                (candidate/'compose.customer.yml').touch();(candidate/'scripts/customer.sh').touch();(candidate/'config/customer.env').touch()
            result=run('');assert result.returncode!=0 and b'exactly one' in result.stderr
        finally:server.shutdown();server.server_close();thread.join()
    print('Launcher shell: engine reuse/start, safe paths, existing images, health, auto-discovery and ambiguity guards passed')


if __name__=='__main__':main()
