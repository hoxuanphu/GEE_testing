import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import cdse, ee_session, main, provider_credentials


_API = '/api/local-provider-settings'
_ORIGIN = 'http://127.0.0.1:8000'


class TestProviderSettingsAccess(unittest.TestCase):
    def test_remote_clients_and_rebound_hosts_cannot_open_settings(self):
        for address, host in [('192.0.2.10', 'localhost'), ('127.0.0.1', 'example.test')]:
            with self.subTest(address=address, host=host):
                client = TestClient(main.app, base_url=f'http://{host}:8000', client=(address, 50000))
                for path in (_API, f'{_API}/availability'):
                    self.assertEqual(client.get(path).status_code, 403)

    def test_cross_origin_submission_is_rejected_without_echoing_credentials(self):
        client = TestClient(main.app, base_url='http://localhost:8000', client=('127.0.0.1', 50000))
        response = client.put('/api/local-provider-settings/cdse',
                              headers={'Origin': 'http://localhost:9000'},
                              json={'username': 'someone@example.test', 'password': 'example-password'})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('example-password', response.text)


class TestProviderSettings(unittest.TestCase):
    def setUp(self):
        local_dir = tempfile.TemporaryDirectory()
        self.addCleanup(local_dir.cleanup)
        self.directory = Path(local_dir.name)
        self.enterContext(patch.object(provider_credentials, '_SETTINGS_DIR', self.directory))
        provider_credentials.active_credentials.cache_clear()
        self.addCleanup(provider_credentials.active_credentials.cache_clear)
        self.client = TestClient(main.app, base_url=_ORIGIN, client=('127.0.0.1', 50000))

    def save(self, provider, payload):
        return self.client.put(f'{_API}/{provider}', json=payload, headers={'Origin': _ORIGIN})

    def test_banner_reports_only_missing_fields_and_separates_staged_keys(self):
        with patch.dict(os.environ, {'CDSE_USERNAME': 'private@example.test', 'Secret_Key_S3': 'private-secret'},
                        clear=True), patch.object(ee_session, '_BASE_DIR', self.directory), \
             patch.object(ee_session, '_initialized', False), \
             patch.object(ee_session.ee.oauth, 'get_credentials_arguments', side_effect=FileNotFoundError), \
             patch.object(ee_session._cloud_sdk, 'get_application_default_credentials_path',
                          return_value=self.directory / 'adc.json'):
            response = self.client.get(f'{_API}/availability')
            self.assertEqual(response.json(), {'missing': {
                'gee': ['service_account_json'], 'cdse': ['password'], 's3': ['access_key'],
            }, 'restart_required': []})
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertNotIn('private', response.text)
            self.assertEqual(self.save('s3', {'access_key': 'saved-key', 'secret_key': 'saved-secret'}).status_code, 200)
            self.assertEqual(self.client.get(f'{_API}/availability').json(), {'missing': {
                'gee': ['service_account_json'], 'cdse': ['password'],
            }, 'restart_required': ['s3']})
            with patch.object(ee_session.ee.oauth, 'get_credentials_arguments', return_value={'refresh_token': 'local'}):
                self.assertNotIn('gee', self.client.get(f'{_API}/availability').json()['missing'])

    def test_cdse_save_is_write_only_and_applies_on_next_start(self):
        payload = {'username': 'someone@example.test', 'password': 'fixture-password=42'}
        self.assertEqual(self.client.get(_API).json()['cdse'], 'not_saved')
        response = self.save('cdse', payload)
        self.assertEqual(response.json(), {'state': 'restart_required'})
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get(_API).json()['cdse'], 'restart_required')
        self.assertIsNone(provider_credentials.active_credentials('cdse'))

        provider_credentials.active_credentials.cache_clear()
        status = self.client.get(_API)
        self.assertEqual(status.json()['cdse'], 'active')
        self.assertNotIn(payload['password'], status.text)
        self.assertNotIn(payload['username'], status.text)
        with patch.object(cdse, '_ensure_env'), patch.object(cdse, '_token', {'access': None, 'expires_at': 0}), \
             patch.object(cdse.requests, 'post', return_value=Mock(json=lambda: {'access_token': 'fixture-token'})) as fetch:
            self.assertEqual(cdse.get_token(), 'fixture-token')
        self.assertEqual(fetch.call_args.kwargs['data']['password'], payload['password'])
        self.assertEqual(fetch.call_args.kwargs['data']['username'], payload['username'])

    def test_s3_resolves_its_own_saved_keys_and_preserves_legacy_env(self):
        with patch.object(cdse, '_ensure_env'), patch.object(cdse, '_S3_CREDS_CACHE', None), patch.dict(
            os.environ, {'Access_Key_S3': 'existing-access', 'Secret_Key_S3': 'existing-secret'}
        ):
            self.assertEqual(cdse.get_s3_credentials(), ('existing-access', 'existing-secret'))
            response = self.save('s3', {'access_key': 'fixture-access', 'secret_key': 'fixture-secret'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(cdse.get_s3_credentials(force_new=True), ('existing-access', 'existing-secret'))
            provider_credentials.active_credentials.cache_clear()
            self.assertEqual(cdse.get_s3_credentials(force_new=True), ('fixture-access', 'fixture-secret'))
        self.assertFalse((self.directory / '.env.providers.cdse.json').exists())

    def test_gee_uses_project_from_json_and_the_google_token_url(self):
        payload = {'type': 'service_account', 'project_id': 'fixture-project',
                   'client_email': 'fixture@fixture-project.iam.gserviceaccount.com',
                   'private_key_id': 'fixture-key-id', 'private_key': 'fixture-key',
                   'token_uri': 'https://untrusted.example.test/token'}
        rejected = self.save('gee', payload)
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn('fixture-key', rejected.text)
        self.assertFalse((self.directory / '.env.providers.gee.json').exists())

        with patch.object(ee_session.ee, 'ServiceAccountCredentials') as credentials:
            self.assertEqual(self.save('gee', payload).status_code, 200)
            provider_credentials.active_credentials.cache_clear()
            with patch.object(ee_session, '_initialized', False), patch.object(ee_session, '_load_dotenv'), \
                 patch.object(ee_session, '_load_extra_cas'), patch.object(ee_session.ee, 'Initialize') as initialize:
                ee_session.init_ee()
        self.assertEqual(json.loads(credentials.call_args.kwargs['key_data'])['token_uri'],
                         'https://oauth2.googleapis.com/token')
        initialize.assert_called_once_with(credentials.return_value, project='fixture-project')

    def test_rejected_payloads_and_failed_replacement_leave_the_previous_file(self):
        payload = {'access_key': 'fixture-access', 'secret_key': 'fixture-secret'}
        self.assertEqual(self.save('s3', payload).status_code, 200)
        path = self.directory / '.env.providers.s3.json'
        previous = path.read_bytes()
        oversized = self.save('s3', {'secret_key': 'x' * 32768})
        self.assertEqual(oversized.status_code, 413)
        invalid = self.save('s3', {'secret_key': 'extra-private'})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn('extra-private', invalid.text)
        with patch.object(Path, 'replace', side_effect=OSError('private-path')):
            failed = self.save('s3', {**payload, 'secret_key': 'replacement'})
        self.assertEqual(failed.status_code, 500)
        self.assertNotIn('private-path', failed.text)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(list(self.directory.iterdir()), [path])
