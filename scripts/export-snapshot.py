#!/usr/bin/env python3
"""Export a bounded directory tree from a consistent snapshot of a live audit DB."""
import argparse
import datetime
import json
import os
import sqlite3
from urllib.parse import quote


def export(dbpath, cluster, filesystem, max_depth=5, max_entries=12000):
    db = sqlite3.connect('file:' + quote(os.path.abspath(dbpath)) + '?mode=ro', uri=True)
    db.execute('PRAGMA cache_size=-8192')
    db.execute('BEGIN')
    try:
        config = {k: json.loads(v) for k,v in db.execute('SELECT key,value FROM meta')}
        root = os.fsencode(config['root'])
        entries = {}
        truncated = False
        # Parents are discovered before children. Keep ancestors even when capped.
        for path,state,files,logical,allocated,errors in db.execute('SELECT path,state,files,logical,allocated,errors FROM dirs ORDER BY id'):
            relative = os.path.relpath(path, root)
            parts = [] if relative == b'.' else relative.split(b'/')
            for depth in range(min(len(parts), max_depth) + 1):
                name = os.fsdecode(b'/'.join(parts[:depth])) if depth else '.'
                if name not in entries:
                    if len(entries) >= max_entries:
                        truncated = True
                        break
                    parent = os.fsdecode(b'/'.join(parts[:depth-1])) if depth > 1 else '.' if depth == 1 else None
                    entries[name] = dict(path=name,parent=parent,files=0,logical=0,allocated=0,errors=0,complete=True)
                entry = entries[name]
                for key,value in [('files',files),('logical',logical),('allocated',allocated),('errors',errors)]:
                    entry[key] += value
                entry['complete'] &= state == 'done' and errors == 0
            if len(parts)>max_depth:
                truncated=True
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        complete = bool(config.get('finished')) and entries.get('.',{}).get('complete',False)
        return dict(cluster=cluster,root=config['root'],filesystem=filesystem,updated_at=now,
                    started_at=datetime.datetime.fromtimestamp(config['started'],datetime.timezone.utc).isoformat(),
                    finished_at=datetime.datetime.fromtimestamp(config['finished'],datetime.timezone.utc).isoformat() if config.get('finished') else None,
                    complete=complete,status='Scan complete' if complete else 'Scan finished with errors' if config.get('finished') else 'Scanning — partial results',
                    entries=list(entries.values()),truncated=truncated,max_depth=max_depth)
    finally:
        db.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True)
    p.add_argument('--cluster',choices=['h100','h200'],required=True)
    p.add_argument('--filesystem',default='Unknown')
    p.add_argument('--max-depth',type=int,default=5)
    p.add_argument('--max-entries',type=int,default=12000)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    if args.max_depth<1 or not 1<=args.max_entries<=15000:p.error('Invalid depth or entry limit')
    result=export(args.db,args.cluster,args.filesystem,args.max_depth,args.max_entries)
    with open(args.output+'.tmp','w') as f:json.dump(result,f)
    os.replace(args.output+'.tmp',args.output)
    print(json.dumps({'entries':len(result['entries']),'complete':result['complete'],'files':result['entries'][0]['files']}))


if __name__=='__main__':main()
