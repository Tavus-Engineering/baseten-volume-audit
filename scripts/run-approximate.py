#!/usr/bin/env python3
"""Run a bounded sampling pass; publish with publish-snapshots.py --snapshot."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--root',default='/root/.cache/team_artifacts')
p.add_argument('--directory',required=True)
p.add_argument('--cluster',required=True)
p.add_argument('--filesystem',default='Unknown')
p.add_argument('--rate',type=int,default=10000)
p.add_argument('--duration',type=int,default=7200)
args=p.parse_args()
base=Path(args.directory);base.mkdir(parents=True,exist_ok=True)
scanner=Path(__file__).resolve().parents[1]/'volume_audit.py'
proc=subprocess.Popen([sys.executable,str(scanner),'scan',args.root,'--approximate','--cluster',args.cluster,
                       '--filesystem',args.filesystem,'--rate',str(args.rate),'--output',str(base/'approximate.json')])
try:
    code=proc.wait(timeout=args.duration)
except subprocess.TimeoutExpired:
    proc.terminate()
    try:code=proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill();code=proc.wait()
(base/'approximate-finished.json').write_text(json.dumps({'exit_code':code,'finished_at':time.time()}))
print('Sampling process exited:',code,flush=True)
