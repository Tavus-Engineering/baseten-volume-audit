import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('export_snapshot',ROOT/'scripts/export-snapshot.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class SnapshotTests(unittest.TestCase):
    def test_aggregate_capped_tree_and_live_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'volume';root.mkdir();db=base/'audit.db'
            for directory in ('a','a/deep','a/deep/leaf','b','b/deep'):
                d=root/directory;d.mkdir();(d/'file').write_bytes(b'1234567')
            subprocess.run([sys.executable,str(ROOT/'volume_audit.py'),'scan',str(root),'--db',str(db)],check=True,capture_output=True)
            full=module.export(str(db),'h100','test')
            entries={e['path']:e for e in full['entries']}
            self.assertEqual(entries['.']['logical'],35)
            self.assertEqual(entries['a']['logical'],21)
            self.assertEqual(entries['b']['logical'],14)
            self.assertTrue(full['complete'])
            capped=module.export(str(db),'h100','test',max_entries=2)
            self.assertEqual(capped['entries'][0]['logical'],35)
            self.assertTrue(capped['truncated'])
            shallow=module.export(str(db),'h100','test',max_depth=1)
            self.assertEqual({e['path'] for e in shallow['entries']},{'.','a','b'})
            self.assertEqual(shallow['entries'][0]['logical'],35)
            with contextlib.closing(sqlite3.connect(db)) as conn:
                conn.execute("DELETE FROM meta WHERE key='finished'")
                conn.execute("UPDATE dirs SET state='pending' WHERE path=?",(os.fsencode((root/'b/deep').resolve()),))
                conn.commit()
            live=module.export(str(db),'h100','test')
            self.assertFalse(live['complete'])
            self.assertFalse(live['entries'][0]['complete'])
            self.assertIn('partial',live['status'])

if __name__=='__main__':unittest.main()
