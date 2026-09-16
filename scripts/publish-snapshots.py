#!/usr/bin/env python3
"""Attach a publisher to an already-running audit without restarting its scan."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--directory',required=True)
p.add_argument('--cluster',choices=['h100','h200'],required=True)
p.add_argument('--filesystem',default='Unknown')
p.add_argument('--config',required=True)
p.add_argument('--snapshot',help='Publish this existing JSON instead of exporting the exact database')
p.add_argument('--finished-file',default='finished.json')
p.add_argument('--final-marker',default='published-final.json')
args=p.parse_args()
base=Path(args.directory);exporter=Path(__file__).with_name('export-snapshot.py')
while True:
    try:
        if not args.snapshot:
            subprocess.run([sys.executable,str(exporter),'--db',str(base/'audit.db'),'--cluster',args.cluster,'--filesystem',args.filesystem,'--output',str(base/'upload.json')],check=True)
        data=json.loads(Path(args.snapshot or base/'upload.json').read_text())
        finished=base/args.finished_file
        if finished.exists():
            code=json.loads(finished.read_text())['exit_code']
            if code not in (0,2):
                data['complete']=False
                data['status']=('Sampling paused — partial estimates' if code==130 else 'Sampling failed — partial estimates') if args.snapshot else ('Scan paused — partial results' if code==130 else 'Scan failed — partial results')
        config=json.loads(Path(args.config).read_text())
        req=urllib.request.Request(config['url'],data=json.dumps(data).encode(),headers={'User-Agent':'TavusVolumeAudit/1.0','Content-Type':'application/json','Authorization':'Bearer '+config['token']},method='POST')
        with urllib.request.urlopen(req,timeout=60) as response:print('Published',response.status,data['updated_at'],flush=True)
        if finished.exists():
            (base/args.final_marker).write_text(json.dumps({'updated_at':data['updated_at'],'complete':data['complete']}))
            break
    except Exception as exc:print('Publication retry:',type(exc).__name__,flush=True)
    time.sleep(60)
