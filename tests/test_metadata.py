import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('metadata', Path(__file__).parents[1] / 'scripts/enrich-metadata.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MetadataTests(unittest.TestCase):
    def test_enrichment_preserves_inventory_and_handles_missing_and_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'root'
            root.mkdir()
            (root / 'child').mkdir()
            (root / 'link').symlink_to(Path(temp), target_is_directory=True)
            snapshot = dict(root=str(root), updated_at='original', complete=True,
                            entries=[dict(path=p, logical=42) for p in ['.', 'child', 'missing', '../escape', 'link', '/tmp']])
            with patch.object(m, 'birth_time', return_value=None):
                result = m.enrich(snapshot, root.stat().st_ino, rate=100000)
            self.assertEqual(result['updated_at'], 'original')
            self.assertTrue(result['complete'])
            for e in result['entries']:
                self.assertEqual(e['logical'], 42)
            self.assertEqual(result['entries'][0]['metadata']['uid'], root.stat().st_uid)
            self.assertIsNone(result['entries'][0]['metadata']['created_at'])
            for e in result['entries'][2:]:
                self.assertIn('error', e['metadata'])
            with self.assertRaisesRegex(ValueError, 'inode mismatch'):
                m.enrich(snapshot, -1)

    def test_birthtime_does_not_substitute_ctime(self):
        from types import SimpleNamespace
        with patch.object(m.sys, 'platform', 'unsupported'):
            self.assertIsNone(m.birth_time(-1, SimpleNamespace(st_ctime=123)))


if __name__ == '__main__':
    unittest.main()
