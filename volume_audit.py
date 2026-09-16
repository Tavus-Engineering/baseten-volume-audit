#!/usr/bin/env python3
"""Metadata-only POSIX volume audit. Python 3.9+, standard library only."""
import argparse
import collections
import concurrent.futures
import contextlib
import fcntl
import heapq
import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time

VERSION = 1
BATCH = 1000
CATEGORIES = {
    'models': {'.pt', '.pth', '.ckpt', '.safetensors', '.onnx', '.gguf'},
    'video': {'.mp4', '.mov', '.mkv', '.webm', '.avi'},
    'audio': {'.wav', '.mp3', '.flac', '.ogg', '.m4a'},
    'images': {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff'},
    'archives': {'.zip', '.tar', '.gz', '.zst', '.bz2', '.xz', '.7z'},
    'datasets': {'.parquet', '.arrow', '.npy', '.npz', '.h5', '.hdf5', '.tfrecord'},
    'logs-text': {'.log', '.txt', '.json', '.jsonl', '.csv'},
}
EXTENSIONS = {ext.encode(): category for category, exts in CATEGORIES.items() for ext in exts}
METRICS = ('files', 'logical', 'allocated', 'symlinks', 'special', 'hardlink_paths', 'errors', 'excluded')
SCHEMA = '''
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE dirs (
 id INTEGER PRIMARY KEY, parent INTEGER, path BLOB UNIQUE NOT NULL,
 depth INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 files INTEGER DEFAULT 0, logical INTEGER DEFAULT 0, allocated INTEGER DEFAULT 0,
 symlinks INTEGER DEFAULT 0, special INTEGER DEFAULT 0, hardlink_paths INTEGER DEFAULT 0,
 errors INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0);
CREATE INDEX dirs_queue ON dirs(state, id);
CREATE INDEX dirs_parent ON dirs(parent);
CREATE INDEX dirs_depth ON dirs(depth);
CREATE TABLE groups (kind TEXT, bucket TEXT, files INTEGER, logical INTEGER, allocated INTEGER,
 PRIMARY KEY(kind,bucket));
CREATE TABLE largest (path BLOB PRIMARY KEY, logical INTEGER, allocated INTEGER, mtime REAL);
CREATE TABLE errors (path BLOB, message TEXT);
'''


def connect(path, readonly=False):
    if readonly:
        from urllib.parse import quote
        db = sqlite3.connect('file:' + quote(os.path.abspath(path)) + '?mode=ro', uri=True, timeout=60)
    else:
        db = sqlite3.connect(path, timeout=60)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA cache_size=-8192')
    db.execute('PRAGMA temp_store=FILE')
    return db


def meta(db):
    return {k: json.loads(v) for k, v in db.execute('SELECT key,value FROM meta')}


def put_meta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))


def printable(path):
    return os.fsdecode(path)


def age_bucket(now, mtime):
    days = (now - mtime) / 86400
    if days < 0:
        return 'future-mtime'
    for threshold in (7, 30, 90, 180, 365):
        if days < threshold:
            return f'<{threshold}d'
    return '>=365d'


class Limiter:
    def __init__(self, rate):
        self.rate = rate
        self.next = time.monotonic()
        self.lock = threading.Lock()

    def wait(self, stop):
        if not self.rate:
            return stop.is_set()
        with self.lock:
            now = time.monotonic()
            delay = max(0, self.next - now)
            self.next = max(self.next, now) + 1 / self.rate
        return stop.wait(delay)


class Interrupted(Exception):
    pass


def scan_directory(dbpath, row, config, stop, limiter):
    """Children are committed in batches but cannot run until this parent finishes."""
    ident, parent, path, depth = row
    db = connect(dbpath)
    counts = collections.Counter()
    groups = collections.defaultdict(lambda: [0, 0, 0])
    largest = []
    errors = []
    children = []

    def error(p, exc):
        counts['errors'] += 1
        if len(errors) < 100:
            errors.append((p, str(exc)))

    def flush():
        if children:
            with db:
                db.executemany('INSERT OR IGNORE INTO dirs(parent,path,depth) VALUES (?,?,?)', children)
            children.clear()

    try:
        # O_NOFOLLOW protects the final component from replacement with a symlink.
        # Namespace changes in ancestors still require a quiescent tree/snapshot.
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            own = os.fstat(fd)
            if own.st_dev != config['device']:
                counts['excluded'] += 1
            else:
                # Directory st_blocks can already be recursive on JuiceFS Enterprise.
                if config['include_directory_blocks']:
                    counts['allocated'] += own.st_blocks * 512
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if limiter.wait(stop):
                            raise Interrupted()
                        name = os.fsencode(entry.name)
                        if depth == 0 and int.from_bytes(hashlib.sha256(name).digest()[:8], 'big') % config['shards'] != config['shard_index']:
                            continue
                        child = os.path.join(path, name)
                        relative = os.path.relpath(child, os.fsencode(config['root'])) if config['exclude_bytes'] else b''
                        if any(relative == x or relative.startswith(x + b'/') for x in config['exclude_bytes']):
                            counts['excluded'] += 1
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            error(child, exc)
                            continue
                        if st.st_dev != config['device']:
                            counts['excluded'] += 1
                            continue
                        if stat.S_ISDIR(st.st_mode):
                            children.append((ident, child, depth + 1))
                            if len(children) >= BATCH:
                                flush()
                            continue
                        counts['allocated'] += st.st_blocks * 512
                        if stat.S_ISLNK(st.st_mode):
                            counts['symlinks'] += 1
                            continue
                        if not stat.S_ISREG(st.st_mode):
                            counts['special'] += 1
                            continue
                        counts['files'] += 1
                        counts['logical'] += st.st_size
                        counts['hardlink_paths'] += int(st.st_nlink > 1)
                        category = EXTENSIONS.get(os.path.splitext(name)[1].lower(), 'other')
                        for kind, bucket in [('type', category), ('mtime_age', age_bucket(config['started'], st.st_mtime))]:
                            g = groups[kind, bucket]
                            g[0] += 1
                            g[1] += st.st_size
                            g[2] += st.st_blocks * 512
                        item = (st.st_size, child, st.st_blocks * 512, st.st_mtime)
                        if st.st_size < config.get('minimum_top_size', 0):
                            continue
                        if len(largest) < config['top']:
                            heapq.heappush(largest, item)
                        elif item > largest[0]:
                            heapq.heapreplace(largest, item)
        finally:
            os.close(fd)
    except OSError as exc:
        error(path, exc)
    except BaseException:
        db.close()
        raise
    try:
        if stop.is_set():
            raise Interrupted()
        flush()
        return (ident, counts, groups, largest, errors)
    finally:
        db.close()


def commit_results(db, results, config):
    """Persist a bounded batch atomically; unfinished parents remain replayable."""
    with db:
        for ident, counts, groups, largest, errors in results:
            db.execute("UPDATE dirs SET state='done'," + ','.join(f'{m}=?' for m in METRICS) + ' WHERE id=?',
                       [counts[m] for m in METRICS] + [ident])
            db.executemany('INSERT INTO groups VALUES (?,?,?,?,?) ON CONFLICT(kind,bucket) DO UPDATE SET '
                           'files=files+excluded.files, logical=logical+excluded.logical, allocated=allocated+excluded.allocated',
                           [(kind, bucket, *values) for (kind, bucket), values in groups.items()])
            db.executemany('INSERT OR REPLACE INTO largest VALUES (?,?,?,?)',
                           [(p, size, allocated, mtime) for size, p, allocated, mtime in largest
                            if size >= config.get('minimum_top_size', 0)])
            if errors:
                remaining = max(0, 10000 - db.execute('SELECT COUNT(*) FROM errors').fetchone()[0])
                db.executemany('INSERT INTO errors VALUES (?,?)', errors[:remaining])
        db.execute('DELETE FROM largest WHERE path NOT IN '
                   '(SELECT path FROM largest ORDER BY logical DESC,path DESC LIMIT ?)', (config['top'],))
    count, minimum = db.execute('SELECT COUNT(*),MIN(logical) FROM largest').fetchone()
    config['minimum_top_size'] = minimum if count >= config['top'] else 0


@contextlib.contextmanager
def scan_lock(dbpath):
    with open(dbpath + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another scan or rollup is using this database')
        yield


def scan(args):
    root = os.path.realpath(args.root)
    dbpath = os.path.realpath(args.db)
    if os.path.commonpath([root, dbpath]) == root:
        raise ValueError('Store the audit database outside the scanned tree, on local scratch storage')
    rootstat = os.stat(root)
    if not stat.S_ISDIR(rootstat.st_mode):
        raise ValueError('Scan root must be a directory')
    if args.shard_index < 0 or args.shard_index >= args.shards:
        raise ValueError('--shard-index must be in [0, --shards)')
    if args.shards > 1 and (not args.scan_id or not args.cluster):
        raise ValueError('Multiple shards require --scan-id and --cluster')
    if args.shards > 1 and args.include_directory_blocks:
        raise ValueError('Directory blocks are unsupported in sharded scans')
    excludes = sorted(set(args.exclude))
    for exclusion in excludes:
        if exclusion in ('', '.') or os.path.isabs(exclusion) or '..' in exclusion.split('/') or os.path.normpath(exclusion) != exclusion:
            raise ValueError('--exclude must be a normalized root-relative path, without ..')
    with scan_lock(dbpath):
        exists = os.path.exists(dbpath)
        if exists and not args.resume:
            raise ValueError('Database exists; use --resume or choose a fresh --db')
        if not exists and args.resume:
            raise ValueError('Cannot resume a missing database')
        db = connect(dbpath)
        try:
            if not exists:
                db.executescript(SCHEMA)
                with db:
                    for k, v in dict(version=VERSION, root=root, device=rootstat.st_dev, inode=rootstat.st_ino,
                                     started=time.time(), top=args.top, exclude=excludes,
                                     include_directory_blocks=args.include_directory_blocks, cluster=args.cluster,
                                     scan_id=args.scan_id, shards=args.shards, shard_index=args.shard_index).items():
                        put_meta(db, k, v)
                    db.execute('INSERT INTO dirs(path,depth) VALUES (?,0)', (os.fsencode(root),))
            config = meta(db)
            if any(config[k] != v for k, v in dict(version=VERSION, root=root, device=rootstat.st_dev,
                     inode=rootstat.st_ino, top=args.top, exclude=excludes,
                     include_directory_blocks=args.include_directory_blocks, cluster=args.cluster,
                     scan_id=args.scan_id, shards=args.shards, shard_index=args.shard_index).items()):
                raise ValueError('Resume options/root identity do not match the original scan')
            config['exclude_bytes'] = [os.fsencode(x) for x in excludes]
            with db:
                # Children of unfinished directories were never eligible to run.
                db.execute("DELETE FROM dirs WHERE parent IN (SELECT id FROM dirs WHERE state='active')")
                db.execute("UPDATE dirs SET state='pending' WHERE state='active'")
                db.execute('DROP TABLE IF EXISTS totals')
                db.execute("DELETE FROM meta WHERE key IN ('finished','rolled_up')")
            stop = threading.Event()
            limiter = Limiter(args.rate)
            previous = {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            active = {}
            failed = None
            last = time.monotonic()
            completed_files = 0
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                    while True:
                        if not stop.is_set():
                            with db:
                                rows = db.execute("SELECT d.id,d.parent,d.path,d.depth FROM dirs d WHERE d.state='pending' "
                                    "AND (d.parent IS NULL OR EXISTS (SELECT 1 FROM dirs p WHERE p.id=d.parent AND p.state='done')) "
                                    'ORDER BY d.id LIMIT ?', (max(args.workers, 32) - len(active),)).fetchall()
                                db.executemany("UPDATE dirs SET state='active' WHERE id=?", [(r[0],) for r in rows])
                            for row in rows:
                                active[pool.submit(scan_directory, dbpath, row, config, stop, limiter)] = row[0]
                        if not active:
                            break
                        done, _ = concurrent.futures.wait(active, timeout=0.02, return_when=concurrent.futures.ALL_COMPLETED)
                        results = []
                        for future in done:
                            active.pop(future)
                            try:
                                result = future.result()
                                results.append(result)
                                completed_files += result[1]['files']
                            except Interrupted:
                                pass
                            except Exception as exc:
                                failed = exc
                                stop.set()
                        if results:
                            commit_results(db, results, config)
                        if time.monotonic() - last >= args.progress:
                            progress = dict(db.execute('SELECT state,COUNT(*) FROM dirs GROUP BY state'))
                            print(json.dumps({'directories': progress, 'files_committed_this_run': completed_files,
                                              'active_workers': min(args.workers, len(active)),
                                              'inflight_directories': len(active)}), file=sys.stderr, flush=True)
                            last = time.monotonic()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            if failed:
                raise failed
            if stop.is_set():
                print('Stopped; rerun the same command with --resume.', file=sys.stderr)
                return 130
            with db:
                put_meta(db, 'finished', time.time())
            errors = db.execute('SELECT COALESCE(SUM(errors),0) FROM dirs').fetchone()[0]
            print(json.dumps({'scan_finished': True, 'errors': errors, 'db': dbpath}))
            return 2 if errors else 0
        finally:
            db.close()


def rollup(args):
    with scan_lock(os.path.realpath(args.db)):
        if not os.path.isfile(args.db):
            raise ValueError('Audit database does not exist')
        db = connect(args.db)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DROP TABLE IF EXISTS totals')
                db.execute('CREATE TABLE totals (id INTEGER PRIMARY KEY, files INTEGER, logical INTEGER, allocated INTEGER, errors INTEGER)')
                db.execute('INSERT INTO totals SELECT id,files,logical,allocated,errors FROM dirs')
                maximum = db.execute('SELECT MAX(depth) FROM dirs').fetchone()[0]
                for depth in range(maximum, 0, -1):
                    db.execute('''INSERT INTO totals
                        SELECT d.parent,SUM(t.files),SUM(t.logical),SUM(t.allocated),SUM(t.errors)
                        FROM dirs d JOIN totals t ON d.id=t.id WHERE d.depth=? GROUP BY d.parent
                        ON CONFLICT(id) DO UPDATE SET files=files+excluded.files,
                        logical=logical+excluded.logical,allocated=allocated+excluded.allocated,errors=errors+excluded.errors''', (depth,))
                put_meta(db, 'rolled_up', time.time())
            print('Rollup ready; use the report command.')
        finally:
            db.close()
    return 0


def report(args):
    db = connect(args.db, readonly=True)
    try:
        # One read snapshot for internally consistent live reports.
        db.execute('BEGIN')
        config = meta(db)
        totals = dict(zip(METRICS, db.execute('SELECT ' + ','.join(f'COALESCE(SUM({m}),0)' for m in METRICS) + ' FROM dirs').fetchone()))
        states = dict(db.execute('SELECT state,COUNT(*) FROM dirs GROUP BY state'))
        result = {'root': config['root'], 'started': config['started'], 'finished': config.get('finished'),
                  'config': config,
                  'complete': bool(config.get('finished')) and not totals['errors'],
                  'directories': states, 'totals': totals,
                  'accounting': 'Path-based; hard links counted per path. Logical bytes: regular files only. Allocated bytes: st_blocks*512; directory blocks excluded unless explicitly enabled. Not physical backend usage.',
                  'groups': [dict(zip(('kind', 'bucket', 'files', 'logical', 'allocated'), row)) for row in db.execute('SELECT * FROM groups ORDER BY kind,logical DESC')],
                  'largest_files': [dict(path=printable(p), logical=l, allocated=a, mtime=m) for p,l,a,m in db.execute('SELECT * FROM largest ORDER BY logical DESC LIMIT ?', (args.limit,))],
                  'error_samples': [dict(path=printable(p), error=e) for p,e in db.execute('SELECT * FROM errors LIMIT ?', (args.limit,))]}
        if config.get('rolled_up'):
            target = os.path.normpath(os.path.join(config['root'], args.path))
            row = db.execute('SELECT id FROM dirs WHERE path=?', (os.fsencode(target),)).fetchone()
            if row is None:
                raise ValueError('Report path was not scanned')
            result['directory'] = target
            result['children'] = [dict(path=printable(p), files=f, logical=l, allocated=a, errors=e)
                for p,f,l,a,e in db.execute(f'SELECT d.path,t.files,t.logical,t.allocated,t.errors FROM dirs d '
                    f'JOIN totals t ON d.id=t.id WHERE d.parent=? ORDER BY t.{args.sort} DESC LIMIT ?', (row[0], args.limit))]
            result['largest_directories'] = [dict(path=printable(p), files=f, logical=l, allocated=a, errors=e)
                for p,f,l,a,e in db.execute(f'SELECT d.path,t.files,t.logical,t.allocated,t.errors FROM dirs d '
                    f'JOIN totals t ON d.id=t.id WHERE d.parent IS NOT NULL ORDER BY t.{args.sort} DESC LIMIT ?', (args.limit,))]
        print(json.dumps(result, indent=2, ensure_ascii=True))
    finally:
        db.close()
    return 0


def preflight(args):
    root = os.path.realpath(args.root)
    fs = os.statvfs(root)
    result = {'root': root, 'device': os.stat(root).st_dev,
              'capacity_bytes': fs.f_blocks * fs.f_frsize,
              'used_bytes': (fs.f_blocks - fs.f_bfree) * fs.f_frsize,
              'available_bytes': fs.f_bavail * fs.f_frsize,
              'inodes_total': fs.f_files, 'inodes_free': fs.f_ffree,
              'baseten_cache_paths': {k: os.environ[k] for k in
                  ('BT_TEAM_CACHE_DIR', 'BT_PROJECT_CACHE_DIR', 'BT_CHECKPOINT_DIR') if k in os.environ}}
    try:
        mount = subprocess.run(['findmnt', '-J', '-T', root, '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS'], capture_output=True, text=True, timeout=10)
        result['mount'] = json.loads(mount.stdout) if mount.returncode == 0 else mount.stderr
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        result['mount_detection_error'] = str(exc)
    print(json.dumps(result, indent=2))
    return 0


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError('must be >= 1')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('preflight', help='Identify mount and capacity without walking the tree')
    p.add_argument('root')
    p.set_defaults(func=preflight)
    p = commands.add_parser('scan', help='Scan metadata; persist resumable results outside the target tree')
    p.add_argument('root')
    p.add_argument('--db')
    p.add_argument('--approximate', action='store_true', help='Sample up to 100 children per large folder; write a separate JSON estimate')
    p.add_argument('--sample-size', type=positive, default=100)
    p.add_argument('--seed', type=int, default=0)
    p.add_argument('--output', help='Approximate snapshot JSON destination outside scanned root')
    p.add_argument('--filesystem', default='Unknown')
    p.add_argument('--workers', type=positive, default=2)
    p.add_argument('--cluster', default='', help='Volume/cluster identity, e.g. h200-us-east')
    p.add_argument('--scan-id', default='', help='Shared identifier for one distributed scan')
    p.add_argument('--shards', type=positive, default=1)
    p.add_argument('--shard-index', type=int, default=0)
    p.add_argument('--include-directory-blocks', action='store_true', help='POSIX only; do not use on JuiceFS Enterprise')
    p.add_argument('--rate', type=positive, help='Global entries/second ceiling; omitted means unlimited')
    p.add_argument('--top', type=positive, default=100)
    p.add_argument('--progress', type=positive, default=30)
    p.add_argument('--exclude', action='append', default=[], help='Exact root-relative subtree to omit; repeatable')
    p.add_argument('--resume', action='store_true')
    p.set_defaults(func=scan)
    p = commands.add_parser('rollup', help='Compute recursive directory totals from the audit database')
    p.add_argument('--db', required=True)
    p.set_defaults(func=rollup)
    p = commands.add_parser('report', help='Emit JSON, including live progress and optional directory rollups')
    p.add_argument('--db', required=True)
    p.add_argument('--path', default='.', help='Root-relative directory whose children to compare')
    p.add_argument('--limit', type=positive, default=30)
    p.add_argument('--sort', choices=['allocated', 'logical', 'files'], default='allocated')
    p.set_defaults(func=report)
    args = parser.parse_args()
    try:
        if args.command == 'scan':
            if args.approximate:
                from approximate_audit import run
                return run(args)
            if not args.db:
                raise ValueError('--db is required for exact scans')
            if args.output:
                raise ValueError('--output is only for --approximate')
        return args.func(args)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
