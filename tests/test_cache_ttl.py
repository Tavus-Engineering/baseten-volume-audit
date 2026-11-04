import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cache_ttl import TTL_SECONDS, sweep


class CacheTTLTests(unittest.TestCase):
    def test_expiration_uses_reads_and_writes_and_exact_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            # Future clock makes real ctime old without mocking filesystem stats.
            now = __import__('time').time() + TTL_SECONDS * 3
            cutoff = now - TTL_SECONDS
            for name, atime, mtime in [('old', cutoff - 1, cutoff - 1),
                                      ('read', now, cutoff - 1),
                                      ('written', cutoff - 1, now),
                                      ('boundary', cutoff, cutoff - 1)]:
                Path(root, name).write_text(name)
                os.utime(Path(root, name), (atime, mtime))
            result = sweep(root, tracked_since=now - TTL_SECONDS * 2,
                           now=now, apply=True, readers_quiesced=True)
            self.assertEqual(result['deleted'], 1)
            self.assertEqual(sorted(os.listdir(root)), ['boundary', 'read', 'written'])

    def test_untrusted_history_and_dry_run_cannot_delete(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'old')
            path.write_text('preserve')
            now = __import__('time').time() + TTL_SECONDS * 3
            result = sweep(root, tracked_since=now - TTL_SECONDS + 1, now=now,
                           apply=True, readers_quiesced=True)
            self.assertTrue(result['warming_up'])
            self.assertTrue(path.exists())
            result = sweep(root, tracked_since=now - TTL_SECONDS * 2, now=now)
            self.assertEqual(result['eligible'], 1)
            self.assertTrue(path.exists())
            with self.assertRaises(ValueError):
                sweep(root, tracked_since=now - TTL_SECONDS * 2, now=now, apply=True)

    def test_symlinks_never_escape_and_internal_trash_is_preserved(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            Path(outside, 'keep').write_text('preserve')
            Path(root, 'link').symlink_to(outside, target_is_directory=True)
            Path(root, 'file-link').symlink_to(Path(outside, 'keep'))
            Path(root, '.stats').write_text('internal')
            Path(root, '.jfs.config').write_text('internal')
            Path(root, '.trash').mkdir()
            Path(root, '.trash', 'keep').write_text('preserve')
            now = __import__('time').time() + TTL_SECONDS * 3
            sweep(root, tracked_since=now - TTL_SECONDS * 2, now=now,
                  apply=True, readers_quiesced=True)
            self.assertTrue(Path(outside, 'keep').exists())
            self.assertTrue(Path(root, '.trash', 'keep').exists())
            self.assertTrue(Path(root, 'file-link').is_symlink())
            self.assertTrue(Path(root, '.stats').exists())
            self.assertTrue(Path(root, '.jfs.config').exists())

    def test_changed_candidate_is_rechecked_before_unlink(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'old')
            path.write_text('preserve')
            now = __import__('time').time() + TTL_SECONDS * 3
            original_stat = os.stat
            calls = 0

            def concurrent_read(name, *args, **kwargs):
                nonlocal calls
                if name == 'old':
                    calls += 1
                    if calls == 2:
                        os.utime(path, (now, now))
                return original_stat(name, *args, **kwargs)

            with patch('cache_ttl.os.stat', side_effect=concurrent_read):
                result = sweep(root, tracked_since=now - TTL_SECONDS * 2, now=now,
                               apply=True, readers_quiesced=True)
            self.assertEqual(result['changed'], 1)
            self.assertTrue(path.exists())
