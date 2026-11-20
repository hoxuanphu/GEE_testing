import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import os
import tempfile
import unittest


class AcceptanceFixtureTests(unittest.TestCase):
    def test_server_data_override_is_applied_before_any_store_is_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = ('from app import main; import json; '
                       'print(json.dumps([str(main._DOWNLOADS_DIR), str(main._frame_store.root), '
                       'str(main._timeseries_store.root), str(main._esri_root)]))')
            result = subprocess.run([sys.executable, '-W', 'ignore', '-c', command],
                                    env={**os.environ, 'GEE_DOWNLOADS_DIR': temporary},
                                    capture_output=True, text=True, check=True)
            paths = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(all(Path(p).is_relative_to(temporary) for p in paths), paths)

    def test_audit_fixture_is_read_only_and_retains_legacy_parameters(self):
        from validate_matched_frame_live import load_audit_fixture
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'frames.sqlite3'
            snapshot = {'parameters': {'reference': {'crs': 'EPSG:4326'}, 'dataset': 's1gee'},
                        'scenes': [{'scene_token': 'old', 'eligible': True}]}
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('create table objects(kind text, data text, created real)')
                db.execute('insert into objects values(?,?,?)', ('search', json.dumps(snapshot), 1))
            before = path.read_bytes()
            self.assertEqual(load_audit_fixture(path), snapshot)
            self.assertEqual(path.read_bytes(), before)

    def test_audit_fixture_rejects_new_snapshots_as_legacy_evidence(self):
        from validate_matched_frame_live import load_audit_fixture
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'frames.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('create table objects(kind text, data text, created real)')
                db.execute('insert into objects values(?,?,?)', ('search', json.dumps({
                    'parameters': {'roi_version': 1, 'reference': {}}, 'scenes': []}), 1))
            with self.assertRaisesRegex(ValueError, 'legacy'):
                load_audit_fixture(path)
