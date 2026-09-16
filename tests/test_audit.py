import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import volume_audit as audit

SCRIPT = str(Path(audit.__file__).resolve())


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'data'
        self.root.mkdir()
        self.db = self.base / 'audit.db'

    def run_cli(self, *args, code=0):
        result = subprocess.run([sys.executable, SCRIPT, *map(str, args)], capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stderr)
        return result.stdout

    def report(self):
        return json.loads(self.run_cli('report', '--db', self.db))

    def fixture(self):
        (self.root / 'sub').mkdir()
        (self.root / 'sub' / 'model.pt').write_bytes(b'x' * 120)
        (self.root / 'small\nfile').write_bytes(b'abc')
        os.link(self.root / 'small\nfile', self.root / 'hardlink')
        os.symlink(self.root, self.root / 'cycle')
        os.symlink('/missing', self.root / 'broken')
        with open(self.root / 'sparse', 'wb') as f:
            f.truncate(16 * 1024 * 1024)
        odd = os.fsencode(self.root) + b'/bad-\xff'
        if sys.platform == 'darwin':
            odd = os.fsencode(self.root / 'unicode-λ')
        with open(odd, 'wb') as f:
            f.write(b'hi')
        os.mkfifo(self.root / 'fifo')

    def test_accounting_rollup_and_non_utf8(self):
        self.fixture()
        self.run_cli('scan', self.root, '--db', self.db, '--workers', 4)
        self.run_cli('rollup', '--db', self.db)
        r = self.report()
        self.assertTrue(r['complete'])
        self.assertEqual(r['totals']['files'], 5)
        self.assertEqual(r['totals']['logical'], 16 * 1024 * 1024 + 128)
        self.assertEqual(r['totals']['symlinks'], 2)
        self.assertEqual(r['totals']['special'], 1)
        self.assertEqual(r['totals']['hardlink_paths'], 2)
        self.assertLess(r['totals']['allocated'], r['totals']['logical'])
        self.assertEqual(r['children'][0]['logical'], 120)
        self.assertEqual(sum(g['logical'] for g in r['groups'] if g['kind'] == 'type'), r['totals']['logical'])
        expected = 0
        for base, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs + files:
                p = os.path.join(base, name)
                if not os.path.isdir(p) or os.path.islink(p):
                    expected += os.lstat(p).st_blocks * 512
        self.assertEqual(r['totals']['allocated'], expected)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT logical FROM totals WHERE id=1').fetchone()[0], r['totals']['logical'])

    def test_excludes_top_and_resume_idempotence(self):
        self.fixture()
        args = ('scan', self.root, '--db', self.db, '--exclude', 'sub', '--top', 2)
        self.run_cli(*args)
        before = self.report()
        self.run_cli(*args, '--resume')
        after = self.report()
        self.assertEqual(before['totals'], after['totals'])
        self.assertEqual(len(after['largest_files']), 2)
        self.assertEqual(after['totals']['excluded'], 1)
        self.run_cli('scan', self.root, '--db', self.db, code=1)
        self.run_cli('scan', self.root, '--db', self.db, '--resume', code=1)

    def test_batched_largest_files_preserves_totals_and_ties(self):
        expected = []
        for i in range(96):
            directory = self.root / str(i)
            directory.mkdir()
            for j in range(3):
                path = directory / str(j)
                size = (i * 17 + j) % 43
                path.write_bytes(b'x' * size)
                expected.append((size, os.fsencode(path.resolve())))
        self.run_cli('scan', self.root, '--db', self.db, '--top', 5, '--workers', 4)
        with contextlib.closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT logical,path FROM largest ORDER BY logical DESC,path DESC').fetchall(),
                             sorted(expected, reverse=True)[:5])
        result = self.report()
        self.assertEqual(result['totals']['files'], len(expected))
        self.assertEqual(result['totals']['logical'], sum(size for size, _ in expected))
        self.assertEqual(sum(g['files'] for g in result['groups'] if g['kind'] == 'type'), len(expected))

    def test_sigkill_resume_staged_children(self):
        for i in range(1100):
            d = self.root / str(i)
            d.mkdir()
            (d / 'file').write_bytes(b'123')
        # Force a crash after a directory batch was persisted but before root completion.
        proc = subprocess.Popen([sys.executable, SCRIPT, 'scan', str(self.root), '--db', str(self.db),
                                 '--workers', '1', '--rate', '1000'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            staged = False
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    if not self.db.exists():
                        time.sleep(.01)
                        continue
                    with contextlib.closing(sqlite3.connect(self.db)) as db:
                        staged = db.execute('SELECT COUNT(*) FROM dirs').fetchone()[0] > 1000
                    if staged:
                        break
                except sqlite3.Error:
                    pass
                time.sleep(.01)
            self.assertTrue(staged, 'did not observe staged children')
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        self.run_cli('scan', self.root, '--db', self.db, '--workers', 4, '--resume')
        r = self.report()
        self.assertTrue(r['complete'])
        self.assertEqual(r['totals']['files'], 1100)
        self.assertEqual(r['totals']['logical'], 3300)
        self.assertEqual(r['directories'], {'done': 1101})

    def test_sigterm_then_resume(self):
        for i in range(20):
            (self.root / str(i)).write_text('test')
        proc = subprocess.Popen([sys.executable, SCRIPT, 'scan', str(self.root), '--db', str(self.db), '--rate', '10'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(.5)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 130, stderr)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.assertFalse(self.report()['complete'])
        self.run_cli('scan', self.root, '--db', self.db, '--resume')
        self.assertEqual(self.report()['totals']['files'], 20)

    def test_error_is_visible_and_returns_two(self):
        (self.root / 'gone').mkdir()
        original = audit.os.open
        def deny(path, *args, **kwargs):
            if os.fsencode(path).endswith(b'/gone'):
                raise PermissionError('test permission denied')
            return original(path, *args, **kwargs)
        with mock.patch.object(sys, 'argv', [SCRIPT, 'scan', str(self.root), '--db', str(self.db)]), \
                mock.patch.object(audit.os, 'open', side_effect=deny), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audit.main(), 2)
        r = self.report()
        self.assertFalse(r['complete'])
        self.assertEqual(r['totals']['errors'], 1)
        self.assertIn('permission denied', r['error_samples'][0]['error'])


    def test_distributed_shards_match_single_scan_and_reject_wrong_cluster(self):
        self.fixture()
        for i in range(10):
            (self.root / f'dir{i}').mkdir()
            (self.root / f'dir{i}' / 'file').write_bytes(b'x' * i)
        self.run_cli('scan', self.root, '--db', self.db)
        expected = self.report()
        reports = []
        for i in range(3):
            db = self.base / f'shard{i}.db'
            self.run_cli('scan', self.root, '--db', db, '--cluster', 'h200', '--scan-id', 'test',
                         '--shards', 3, '--shard-index', i)
            output = self.run_cli('report', '--db', db, '--limit', 100)
            path = self.base / f'shard{i}.json'
            path.write_text(output)
            reports.append(str(path))
        command = [sys.executable, str(Path(SCRIPT).parent / 'scripts/merge-reports.py')]
        result = subprocess.run(command + reports, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        merged = json.loads(result.stdout)
        self.assertEqual(merged['totals'], expected['totals'])
        self.assertEqual(merged['largest_files'][0], expected['largest_files'][0])
        incomplete = subprocess.run(command + reports[:2], capture_output=True, text=True)
        self.assertNotEqual(incomplete.returncode, 0)
        r = json.loads(Path(reports[0]).read_text())
        r['config']['cluster'] = 'h100'
        Path(reports[0]).write_text(json.dumps(r))
        wrong = subprocess.run(command + reports, capture_output=True, text=True)
        self.assertNotEqual(wrong.returncode, 0)

    def test_lock_and_output_guard(self):
        with audit.scan_lock(str(self.db)):
            self.run_cli('scan', self.root, '--db', self.db, code=1)
        self.run_cli('scan', self.root, '--db', self.root / 'bad.db', code=1)
        self.run_cli('scan', self.root, '--db', self.db, '--exclude', '../bad', code=1)

    def test_preflight_and_empty(self):
        p = json.loads(self.run_cli('preflight', self.root))
        self.assertGreater(p['capacity_bytes'], 0)
        self.run_cli('scan', self.root, '--db', self.db)
        self.run_cli('rollup', '--db', self.db)
        self.assertTrue(self.report()['complete'])
        self.assertEqual(self.report()['totals']['files'], 0)


if __name__ == '__main__':
    unittest.main()
