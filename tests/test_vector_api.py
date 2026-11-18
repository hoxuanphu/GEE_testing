"""Real multipart import and immutable polygon ROI API without provider access."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.vector_store import VectorStore


class VectorAPITests(unittest.TestCase):
    def setUp(self):
        from app.vector_api import register_vector_routes
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store = VectorStore(Path(temporary.name))
        api = FastAPI(); register_vector_routes(api, self.store)
        self.client = TestClient(api); self.addCleanup(self.client.close)
        self.geometry = {'type':'Polygon','coordinates':[[[107,10],[107.01,10],[107.01,10.01],[107,10.01],[107,10]]]}
        self.data = {'type':'FeatureCollection','features':[
            {'type':'Feature','properties':{'name':'Vùng A'},'geometry':self.geometry},
            {'type':'Feature','properties':{'name':'Điểm A'},'geometry':{'type':'Point','coordinates':[107,10]}}]}

    def upload(self):
        return self.client.post('/api/vectors/import', files={'file':('sample.geojson',json.dumps(self.data).encode(),'application/geo+json')})

    def test_import_select_apply_and_remove_layer_keeps_immutable_roi(self):
        with mock.patch('app.main._ensure_ee', side_effect=AssertionError('import must be offline')):
            response = self.upload()
            self.assertEqual(response.status_code,200,response.text)
            imported = response.json()
            features = imported['feature_collection']['features']
            response = self.client.post('/api/vectors/rois',json={'selections':[
                {'import_id':imported['import_id'],'feature_ids':[f['id'] for f in features]}]})
        self.assertEqual(response.status_code,200,response.text)
        roi = response.json()
        self.assertEqual(roi['geometry']['type'],'Polygon')
        self.assertEqual(roi['excluded_count'],1)
        self.assertEqual(self.client.delete('/api/vectors/imports/'+imported['import_id']).status_code,204)
        self.assertEqual(self.client.get('/api/vectors/imports/'+imported['import_id']).status_code,404)
        self.assertEqual(self.client.get('/api/vectors/rois/'+roi['roi_id']).json()['geometry'],roi['geometry'])

    def test_bad_vector_and_geometry_instead_of_reference_rejected(self):
        response = self.client.post('/api/vectors/import',files={'file':('bad.kml',b'<!DOCTYPE kml [<!ENTITY evil "boom">]><kml>&evil;</kml>')})
        self.assertEqual(response.status_code,422,response.text)
        response = self.client.post('/api/vectors/rois',json={'selections':[], 'geometry':self.geometry})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.client.get('/api/vectors/rois/not-a-uuid').status_code,404)

    def test_missing_file_multiple_files_and_declared_limit_fail_safely(self):
        self.assertEqual(self.client.post('/api/vectors/import',data=b'not multipart').status_code,400)
        response = self.client.post('/api/vectors/import',files=[('file',('a.json',b'{}')),('file',('b.json',b'{}'))])
        self.assertEqual(response.status_code,400)
        response = self.client.post('/api/vectors/import',headers={'content-length':str(30*1024*1024)},content=b'x')
        self.assertEqual(response.status_code,413)


if __name__ == '__main__':
    unittest.main()
