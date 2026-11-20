"""Pinned SDK offline contract checks, skipped in base-only installations."""
import importlib.util
import importlib.metadata
import os
import shutil
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import eodag_adapter as adapter


@unittest.skipUnless(importlib.util.find_spec('eodag'), 'Optional EODAG SDK is not installed')
class TestEodagSdk(unittest.TestCase):
    def setUp(self):
        self.assertEqual(importlib.metadata.version('eodag'), '4.9.2')
        self.directory = Path(__file__).resolve().parent.parent / 'downloads' / ('eodag-sdk-test-' + uuid.uuid4().hex)
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))

    def test_local_provider_configuration_mapping_and_ordering(self):
        for provider, collection in adapter.COLLECTIONS.items():
            with self.subTest(provider=provider), patch.dict(os.environ, EODAG_CONFIG_FILE=''), \
                 patch('app.ee_session._load_extra_cas'):
                gateway = adapter._gateway(self.directory / provider, provider, time.monotonic() + 120)
                plugins, kwargs = gateway._prepare_search(collection=collection, provider=provider,
                                    sort_by=[('start_datetime', 'DESC')], limit=100)
                self.assertEqual(len(plugins), 1)
                plugin = plugins[0]
                self.assertEqual(plugin.provider, provider)
                self.assertEqual(plugin.config.products[collection]['_collection'], adapter.NATIVE_COLLECTION)
                self.assertIn('start_datetime', plugin.config.sort['sort_param_mapping'])
                self.assertEqual(gateway.settings.cfg_dir, self.directory / provider)

    def test_real_next_page_uses_bounded_gateway_and_explicit_provider(self):
        from eodag.api.search_result import SearchResult
        from eodag.api.core import EODataAccessGateway

        with patch.dict(os.environ, EODAG_CONFIG_FILE=''), patch('app.ee_session._load_extra_cas'):
            gateway = adapter._gateway(self.directory, 'earth_search', time.monotonic() + 10)
        first_product = SimpleNamespace(provider='earth_search', properties={'id': 'one'})
        next_product = SimpleNamespace(provider='earth_search', properties={'id': 'two'})
        params = dict(provider='earth_search', collection='S2_MSI_L2A_COG', limit=100)
        first = SearchResult([first_product], search_params=params, next_page_token='next', raise_errors=True)
        following = SearchResult([next_product], search_params=dict(params), raise_errors=True)
        first._dag = gateway
        plugin = SimpleNamespace(provider='earth_search', config=SimpleNamespace(
            sort={'sort_param_mapping': {'start_datetime': 'properties.datetime'}}))
        with patch.object(gateway, '_prepare_search', return_value=([plugin], dict(params))) as prepare, \
             patch.object(EODataAccessGateway, '_do_search', return_value=following):
            result = next(first.next_page(update=False))
        self.assertIs(result, following)
        self.assertEqual(len(first), 1)
        self.assertEqual(prepare.call_args.kwargs['provider'], 'earth_search')
        self.assertLessEqual(plugin.config.timeout, 10)
        self.assertEqual(plugin.config.retry_total, 0)
        self.assertTrue(plugin.config.ssl_verify)
        self.assertIs(gateway.last_page, following)


if __name__ == '__main__':
    unittest.main()
