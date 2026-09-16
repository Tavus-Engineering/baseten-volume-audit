#!/usr/bin/env python3
"""Run a bounded scan and periodically export/publish its directory snapshot.

Optional --publish-config is a private JSON file containing url and token.
It is re-read at every publication so it can be supplied after scan startup.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--root',default='/root/.cache/team_artifacts')
p.add_argument('--cluster',choices=['h100','h200'],required=True)
p.add_argument('--filesystem',default='Unknown')
p.add_argument('--directory',required=True)
p.add_argument('--duration',type=int,default=6000)
p.add_argument('--rate',type=int,default=10000)
p.add_argument('--workers',type=int,default=4)
p.add_argument('--publish-config')
args=p.parse_args()
base=Path(args.directory);base.mkdir(parents=True,exist_ok=True)
repo=Path(__file__).resolve().parents[1]
db=base/'audit.db';snapshot=base/'snapshot.json'
command=[sys.executable,str(repo/'volume_audit.py'),'scan',args.root,'--db',str(db),'--cluster',args.cluster,'--rate',str(args.rate),'--workers',str(args.workers)]
if db.exists():command.append('--resume')
proc=subprocess.Popen(command)
start=time.monotonic()

def publish(final_code=None):
    if not db.exists():return
    result=subprocess.run([sys.executable,str(repo/'scripts/export-snapshot.py'),'--db',str(db),'--cluster',args.cluster,'--filesystem',args.filesystem,'--output',str(snapshot)],capture_output=True,text=True)
    if result.returncode:
        print('Snapshot not ready:',result.stderr[:300],flush=True);return
    if final_code is not None and final_code not in (0,2):
        data=json.loads(snapshot.read_text())
        data['complete']=False
        data['status']='Scan paused — partial results' if final_code==130 else 'Scan failed — partial results'
        snapshot.write_text(json.dumps(data))
    print(result.stdout.strip(),flush=True)
    if args.publish_config and os.path.exists(args.publish_config):
        try:
            conf=json.load(open(args.publish_config))
            request=urllib.request.Request(conf['url'],data=snapshot.read_bytes(),headers={'Content-Type':'application/json','Authorization':'Bearer '+conf['token']},method='POST')
            with urllib.request.urlopen(request,timeout=60) as response:
                print('Published snapshot:',response.status,flush=True)
        except Exception as exc:print('Publication failed:',type(exc).__name__,flush=True)

try:
    while proc.poll() is None:
        time.sleep(30)
        publish()
        if time.monotonic()-start>args.duration:
            proc.send_signal(signal.SIGTERM)
            break
    try:code=proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill();code=proc.wait()
    publish(code)
    (base/'finished.json').write_text(json.dumps({'exit_code':code,'finished_at':time.time()}))
    print('Audit process exited:',code,flush=True)
finally:
    if proc.poll() is None:
        proc.terminate()
