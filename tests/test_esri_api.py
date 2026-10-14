"""Offline HTTP contracts for Esri jobs."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient
from app import main


class TestEsriAPI(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.patch = patch.object(main, '_esri_service', self.service)
        self.patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.patch.stop()

    def test_create_validate_and_no_earth_engine(self):
        self.service.start.return_value = {'job_id': 'abc', 'status': 'queued'}
        with patch.object(main, '_ensure_ee', side_effect=AssertionError('No GEE')):
            response = self.client.post('/api/esri/jobs', json={'bbox': [105, 21, 105.01, 21.01]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'queued')
        for body in [{}, {'bbox': [105, 21, 104, 22]}, {'bbox': [105, 21, 106, 22], 'zoom': 20},
                     {'commune_id': '1', 'bbox': [105, 21, 106, 22]}, {'commune_id': '1', 'url': 'https://example.com'},
                     {'commune_id': '1', 'workers': 0}, {'commune_id': '1', 'max_tiles': 20001}]:
            self.assertEqual(self.client.post('/api/esri/jobs', json=body).status_code, 422, body)
        self.service.start.side_effect = main.EsriBusyError('busy')
        self.assertEqual(self.client.post('/api/esri/jobs', json={'commune_id': '1'}).status_code, 409)
        self.service.start.side_effect = ValueError('too large')
        self.assertEqual(self.client.post('/api/esri/jobs', json={'commune_id': '1'}).status_code, 400)

    def test_lifespan_recovers_and_shuts_down(self):
        self.service._closed = False
        with TestClient(main.app):
            self.service.recover.assert_called_once()
        self.service.shutdown.assert_called_once()

    def test_status_actions_and_download(self):
        self.service.get.return_value = None
        self.service.cancel.return_value = None
        self.service.resume.return_value = None
        for suffix in ['', '/download']:
            self.assertEqual(self.client.get('/api/esri/jobs/missing' + suffix).status_code, 404)
        for action in ['cancel', 'resume']:
            self.assertEqual(self.client.post('/api/esri/jobs/missing/' + action).status_code, 404)
        self.service.get.return_value = {'job_id': 'abc', 'status': 'running'}
        self.service.download_path.return_value = None
        self.assertEqual(self.client.get('/api/esri/jobs/abc/download').status_code, 409)
        self.service.cancel.return_value = {'status': 'cancelled'}
        self.assertEqual(self.client.post('/api/esri/jobs/abc/cancel').json()['status'], 'cancelled')
        self.service.resume.side_effect = main.EsriBusyError('busy')
        self.assertEqual(self.client.post('/api/esri/jobs/abc/resume').status_code, 409)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.tif'
            output.write_bytes(b'test-tiff')
            self.service.download_path.return_value = output
            response = self.client.get('/api/esri/jobs/abc/download')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b'test-tiff')


if __name__ == '__main__':
    unittest.main()
