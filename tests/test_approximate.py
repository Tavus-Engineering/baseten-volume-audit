import contextlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from approximate_audit import expanded

class ApproximateTests(unittest.TestCase):
    def test_variance_propagation(self):
        self.assertEqual(expanded([(10,0),(20,0)],4),(60,200))
        self.assertEqual(expanded([(10,4),(20,9)],2),(30,13))
        self.assertEqual(expanded([(10,0)],4),(40,None))

    def run_scan(self,root,out,*extra):
        result=subprocess.run([sys.executable,str(ROOT/'volume_audit.py'),'scan',str(root),'--approximate','--output',str(out),*extra],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(out.read_text())

    def test_sampled_folders_extrapolate_counts_and_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'root';dataset=root/'dataset';dataset.mkdir(parents=True)
            for i in range(150):
                d=dataset/str(i);d.mkdir()
                for j in range(3):(d/str(j)).write_bytes(b'x'*10)
            data=self.run_scan(root,base/'estimate.json')
            entries={e['path']:e for e in data['entries']}
            self.assertEqual(entries['.']['files'],450)
            self.assertEqual(entries['.']['logical'],4500)
            self.assertTrue(entries['.']['estimated'])
            self.assertFalse(data['complete'])
            self.assertTrue(data['coverage_complete'])
            self.assertEqual(entries['dataset']['direct_directories'],150)
            self.assertEqual(entries['dataset']['sampled_directories'],100)
            self.assertEqual(data['counters']['file_stats'],300)
            self.assertLess(data['counters']['directories_opened'],152)
            self.assertEqual(set(entries),{'.','dataset'})

    def test_large_flat_folder_counts_files_without_stating_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'root';root.mkdir()
            for i in range(200):(root/str(i)).write_bytes(b'x'*10)
            data=self.run_scan(root,base/'estimate.json')
            self.assertEqual(data['entries'][0]['files'],200)
            self.assertEqual(data['entries'][0]['logical'],2000)
            self.assertEqual(data['counters']['file_stats'],100)

    def test_small_mixed_tree_is_census_and_excludes_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'root';(root/'sub').mkdir(parents=True)
            (root/'one').write_bytes(b'abc');(root/'sub'/'two').write_bytes(b'abcd')
            (root/'cycle').symlink_to(root,target_is_directory=True)
            data=self.run_scan(root,base/'estimate.json')
            self.assertEqual(data['entries'][0]['files'],2)
            self.assertEqual(data['entries'][0]['logical'],7)
            self.assertFalse(data['entries'][0]['estimated'])
            self.assertEqual(data['entries'][0]['logical_margin95'],0)

    def test_stratified_mixed_children_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'root';dataset=root/'data';dataset.mkdir(parents=True)
            for i in range(80):
                (dataset/('f'+str(i))).write_bytes(b'x'*10)
                child=dataset/('d'+str(i));child.mkdir();(child/'f').write_bytes(b'x'*20)
            data=self.run_scan(root,base/'estimate.json')
            d=next(e for e in data['entries'] if e['path']=='data')
            self.assertEqual(d['sampled_files']+d['sampled_directories'],100)
            self.assertEqual(d['files'],160)
            self.assertEqual(d['logical'],2400)

if __name__=='__main__':unittest.main()
