#!/usr/bin/env python3
"""Exercise updater against disposable Git repositories and a fake local service."""
import base64
import http.server
import os
from pathlib import Path
import re
import shutil
import sys
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT/'Start-XianyuIntelligentCs.ps1').read_text()
service = re.search(r"\$ServiceScript = @'\n(.*?)\n'@", source, re.S).group(1)


def main():
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory).resolve();remote=root/'remote.git';author=root/'author';project=root/"deployment with ' quote"
        def git(*args,cwd=None,ok=True):
            return subprocess.run(['git',*map(str,args)],cwd=cwd,check=ok,capture_output=True,text=True)
        git('init','--bare',remote);git('init','-b','main',author)
        def identity(path):
            git('config','user.name','Synthetic',cwd=path);git('config','user.email','fixture@example.invalid',cwd=path)
        identity(author)
        (author/'scripts').mkdir();(author/'compose.customer.yml').write_text('name: fixture\n')
        def app_script(label,fail=False):
            return '#!/bin/bash\ncase "$1" in\nstart|run) echo "'+label+':$1" >> "$TRACE"; '+('exit 9' if fail else ':')+';;\nstatus) :;;\nesac\n'
        (author/'scripts/customer.sh').write_text(app_script('old'))
        git('add','.',cwd=author);git('commit','-m','initial',cwd=author);git('remote','add','origin',remote,cwd=author);git('push','-u','origin','main',cwd=author)
        git('clone','--branch','main',remote,project);identity(project)
        (project/'config').mkdir();(project/'private-note.txt').write_text('preserve untracked')
        binary=root/'bin';binary.mkdir();trace=root/'trace'
        docker=binary/'docker';docker.write_text('#!/bin/sh\n[ "$1" = info ]\n');docker.chmod(0o755)
        if not shutil.which('flock'):
            # macOS fixture shim; CI executes the real Linux flock binary.
            flock=binary/'flock'
            flock.write_text('#!'+sys.executable+'\n'+"import fcntl,os,subprocess,sys\nif sys.argv[1]=='-n':\n try: fcntl.flock(int(sys.argv[2]),fcntl.LOCK_EX|fcntl.LOCK_NB)\n except BlockingIOError: sys.exit(1)\nelse:\n with open(sys.argv[1],'a') as f:\n  fcntl.flock(f,fcntl.LOCK_EX);sys.exit(subprocess.call(sys.argv[2:]))\n")
            flock.chmod(0o755)
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):self.send_response(200);self.end_headers();self.wfile.write(b'{}')
            def log_message(self,*args):pass
        server=http.server.HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        (project/'config/customer.env').write_text('SAAS_CUSTOMER_WEB_PORT='+str(server.server_port)+'\n')
        original_env=(project/'config/customer.env').read_bytes()
        # Only the allowed-origin pattern changes in this extracted test copy.
        body=service.replace('https://github.com/ParseC-ctrl/xianyu-intelligent-cs.git|',str(remote)+'|',1)
        def run():
            prefix="project_encoded='"+base64.b64encode(str(project).encode()).decode()+"'\naction_encoded='dXBkYXRl'\n"
            return subprocess.run(['bash'],input=prefix+body,text=True,capture_output=True,env={**os.environ,'PATH':str(binary)+os.pathsep+os.environ['PATH'],'TRACE':str(trace),'GIT_CONFIG_NOSYSTEM':'1'})
        def publish(label,fail=False):
            (author/'scripts/customer.sh').write_text(app_script(label,fail))
            git('add','.',cwd=author);git('commit','-m',label,cwd=author);git('push','origin','main',cwd=author)
        def fails_without_build(result):
            assert result.returncode!=0,(result.stdout,result.stderr)
            assert '__XIANYU_URL__=' not in result.stdout
            assert not trace.exists()
        try:
            publish('new')
            result=run();assert result.returncode==0,(result.stdout,result.stderr)
            assert trace.read_text()=='new:start\n'
            assert git('rev-parse','HEAD',cwd=project).stdout==git('rev-parse','HEAD',cwd=author).stdout
            assert (project/'config/customer.env').read_bytes()==original_env
            assert (project/'private-note.txt').read_text()=='preserve untracked'
            result=run();assert result.returncode==0;assert trace.read_text().count('new:start')==2
            trace.unlink()
            with (project/'compose.customer.yml').open('a') as f:f.write('# local change\n')
            fails_without_build(run());git('restore','compose.customer.yml',cwd=project)
            git('checkout','--detach',cwd=project);fails_without_build(run());git('checkout','main',cwd=project)
            git('remote','set-url','origin',root/'other.git',cwd=project);fails_without_build(run());git('remote','set-url','origin',remote,cwd=project)
            git('commit','--allow-empty','-m','local only',cwd=project);fails_without_build(run())
            git('reset','--hard','origin/main',cwd=project)  # Disposable fixture only.
            lock=project/'.local/windows-update.lock'
            held=subprocess.Popen([shutil.which('flock') or str(binary/'flock'),str(lock),'sleep','10'])
            try:
                import time
                time.sleep(.15)
                fails_without_build(run())
            finally:held.terminate();held.wait()
            # Allowed remote unavailable: no build and no false success.
            moved=root/'offline.git';remote.rename(moved)
            try:fails_without_build(run())
            finally:moved.rename(remote)
            publish('broken-build',True)
            result=run();assert result.returncode!=0;assert '__XIANYU_URL__=' not in result.stdout
            assert trace.read_text()=='broken-build:start\n'
        finally:server.shutdown();server.server_close();thread.join()
    print('Windows updater: fast-forward, latest script execution, preserved private files, repeated update, dirty/detached/ahead/wrong-origin/concurrent/offline/build-failure guards passed')


if __name__=='__main__':main()
