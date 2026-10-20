"""Offline EODAG pagination, identity, deadline, and metadata contracts."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import eodag_adapter as adapter
from app.catalog_models import CatalogFailure, CatalogUnavailable


def product(identifier='item', provider='earth_search', **props):
    return SimpleNamespace(provider=provider, properties={'id': identifier, 'datetime': '2026-01-20T03:00:00Z',
                           'eo:cloud_cover': 25, **props}, assets={'B02': {'href': 'https://private/?token=secret'}},
                           geometry=None)


class Page(list):
    def __init__(self, products=(), following=None, error=None, token=None):
        super().__init__(products)
        self.following, self.error = following, error
        self.next_page_token = token if token is not None else ('next' if following is not None or error else None)
        self.transitions = 0

    def next_page(self, update=True):
        self.transitions += 1
        if self.error:
            raise self.error
        if self.following is not None:
            yield self.following


class TestEodagAdapter(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, EODAG_ENABLED='true', EODAG_PROVIDERS='earth_search,planetary_computer', EODAG_MAX_RESULTS='4000')
        self.env.start()
        self.addCleanup(self.env.stop)

    def search(self, first, **kwargs):
        gateway = Mock(last_page=None)
        gateway.search.return_value = first
        with patch.object(adapter, '_gateway', return_value=gateway):
            result = adapter.search((105, 9, 107, 11), '2026-01-01', '2026-01-31', provider='earth_search', **kwargs)
        return result, gateway

    def test_explicit_provider_mapping_sort_and_exclusive_end(self):
        for provider, collection in adapter.COLLECTIONS.items():
            with self.subTest(provider=provider), patch.object(adapter, '_gateway') as factory:
                factory.return_value.search.return_value = Page()
                result = adapter.search((105, 9, 107, 11), '2024-02-01', '2024-02-29', provider=provider)
                args = factory.return_value.search.call_args.kwargs
                self.assertEqual(args['provider'], provider)
                self.assertEqual(args['collection'], collection)
                self.assertEqual(args['start'], '2024-02-01T00:00:00Z')
                self.assertEqual(args['end'], '2024-03-01T00:00:00Z')
                self.assertEqual(args['sort_by'], [('start_datetime', 'DESC')])
                self.assertEqual(args['limit'], 100)
                self.assertTrue(args['raise_errors'])
                self.assertFalse(args['validate'])
                self.assertNotIn('cloudCover', args)
                self.assertEqual(result.parameters['completion'], 'complete')

    def test_cloud_filter_continues_pages_deduplicates_and_preserves_zero(self):
        last = Page([product('zero', **{'eo:cloud_cover': 0}), product('good', **{'eo:cloud_cover': 4})])
        second = Page([product('good', **{'eo:cloud_cover': 4}), product('unknown', **{'eo:cloud_cover': None})], last)
        first = Page([product('above', **{'eo:cloud_cover': 90})], second)
        result, _ = self.search(first, cloud_max=5)
        self.assertEqual({r['id'] for r in result.scenes}, {'zero', 'good'})
        self.assertEqual(first.transitions, 1)
        self.assertEqual(second.transitions, 1)
        self.assertEqual(result.parameters['missing_cloud_count'], 1)
        self.assertFalse(result.parameters['truncated'])

    def test_result_cap_after_filtering_and_stable_order(self):
        with patch.dict(os.environ, EODAG_MAX_RESULTS='2'):
            result, _ = self.search(Page([product('excluded', **{'eo:cloud_cover': 90}), product('a'), product('b'), product('c')]), cloud_max=30)
        self.assertEqual([r['id'] for r in result.scenes], ['b', 'a'])
        self.assertEqual(result.parameters['truncation_reason'], 'result_cap')
        self.assertEqual(result.parameters['provider_limit'], 2)

    def test_timestamp_utc_next_day_exclusion_and_date_only(self):
        first = Page([product('before', datetime='2026-01-01T00:30:00+02:00'),
                      product('next-day', datetime='2026-02-01T00:00:00Z'),
                      product('offset', datetime='2026-02-01T01:00:00+02:00'),
                      product('date-only', datetime='2026-01-31')])
        result, _ = self.search(first)
        by_id = {r['id']: r for r in result.scenes}
        self.assertEqual(set(by_id), {'offset', 'date-only'})
        self.assertEqual(by_id['offset']['timestamp'], '2026-01-31T23:00:00Z')
        self.assertIsNone(by_id['date-only']['timestamp'])

    def test_missing_metadata_and_no_transport_values(self):
        p = product(None)
        result, _ = self.search(Page([p, product('missing', **{'eo:cloud_cover': 'NOT_AVAILABLE'}),
                                      product('nan', **{'eo:cloud_cover': float('nan')}),
                                      product('range', **{'eo:cloud_cover': 101}),
                                      product('bad', **{'eo:cloud_cover': 'not-a-number'}), product('valid')]))
        self.assertEqual(result.parameters['missing_id_count'], 1)
        self.assertEqual(result.parameters['missing_cloud_count'], 4)
        row = result.scenes[0]
        for key in ('platform', 'instrument', 'processing_level', 'footprint', 'bbox', 'tile'):
            self.assertIsNone(row[key], key)
        self.assertEqual(row['asset_keys'], ['B02'])
        self.assertNotIn('secret', repr(row))
        self.assertFalse(row['quicklook_available'])
        self.assertEqual(row['download_capabilities'], adapter.CAPABILITY)

    def test_geometry_and_allowlisted_source_metadata(self):
        p = product('safe', **{'grid:code': 'MGRS-48PWS', 's2:processing_baseline': '05.00',
                             's2:product_uri': 'https://private/?sig=secret', 'password': 'secret'})
        p.geometry = {'type': 'Polygon', 'coordinates': [[[105, 9], [107, 9], [107, 11], [105, 9]]]}
        row = adapter.normalize(p, 'earth_search')
        self.assertEqual(row['bbox'], [105, 9, 107, 11])
        self.assertEqual(row['tile'], '48PWS')
        self.assertEqual(row['source_metadata'], {'s2:processing_baseline': '05.00'})
        p.geometry['coordinates'][0][0][0] = float('inf')
        self.assertIsNone(adapter.normalize(p, 'earth_search')['footprint'])

    def test_repeated_page_is_partial_even_without_qualifying_rows(self):
        first = Page([product('repeat', **{'eo:cloud_cover': 90})], Page([product('repeat', **{'eo:cloud_cover': 90})]))
        result, _ = self.search(first, cloud_max=10)
        self.assertEqual(result.scenes, [])
        self.assertEqual(result.parameters['truncation_reason'], 'repeated_page')
        self.assertEqual(result.parameters['completion'], 'partial')

    def test_sdk_suppressed_repetition_is_not_exhaustion(self):
        first = Page([product()], token='next')
        gateway = Mock(last_page=Page([product()]))
        gateway.search.return_value = first
        with patch.object(adapter, '_gateway', return_value=gateway):
            result = adapter.search((1, 2, 3, 4), '2026-01-01', '2026-01-31', provider='earth_search')
        self.assertEqual(result.parameters['truncation_reason'], 'repeated_page')

    def test_failures_are_sanitized_and_partial_after_usable_page(self):
        result, _ = self.search(Page([product()], error=RuntimeError('https://private/?token=secret')))
        self.assertEqual(result.parameters['truncation_reason'], 'provider_error')
        self.assertNotIn('secret', repr(result))
        with patch.object(adapter, '_gateway', side_effect=CatalogUnavailable('EODAG SDK or configuration is unavailable')):
            with self.assertRaises(CatalogUnavailable):
                adapter.search((1, 2, 3, 4), '2026-01-01', '2026-01-31', provider='earth_search')
        with patch.object(adapter, '_gateway') as factory:
            factory.return_value.search.side_effect = RuntimeError('secret')
            with self.assertRaisesRegex(CatalogFailure, '^EODAG provider search failed$'):
                adapter.search((1, 2, 3, 4), '2026-01-01', '2026-01-31', provider='earth_search')

    def test_provider_mismatch_never_falls_back(self):
        with self.assertRaises(CatalogFailure):
            self.search(Page([product(provider='planetary_computer')]))

    def test_page_budget_after_local_filtering(self):
        current = Page([product('199', **{'eo:cloud_cover': 99})], token='more')
        for i in reversed(range(199)):
            current = Page([product(str(i), **{'eo:cloud_cover': 99})], current)
        result, _ = self.search(current, cloud_max=1)
        self.assertEqual(result.scenes, [])
        self.assertEqual(result.parameters['truncation_reason'], 'page_budget')

    def test_deadline_before_call_and_transition(self):
        with patch.object(adapter.time, 'monotonic', return_value=100):
            self.assertEqual(adapter._remaining(110), 10)
            self.assertEqual(adapter._remaining(200), 30)
            with self.assertRaises(adapter.DeadlineExceeded):
                adapter._remaining(100)
        first = Page([product()], Page([product('later')]))
        with patch.object(adapter.time, 'monotonic', side_effect=[0, 0, 1, 120]):
            result, _ = self.search(first)
        self.assertEqual(first.transitions, 0)
        self.assertEqual(result.parameters['truncation_reason'], 'time_budget')
        with patch.object(adapter.time, 'monotonic', side_effect=[0, 120]):
            with self.assertRaises(CatalogFailure):
                self.search(Page())

    def test_sdk_gateway_isolation_timeout_retry_and_tls_controls(self):
        class FakeGateway:
            def __init__(self, settings):
                self.settings = settings

            def _do_search(self, plugin, **kwargs):
                return Page()

        modules = {'eodag': SimpleNamespace(EODataAccessGateway=FakeGateway),
                   'eodag.config': SimpleNamespace(EODAGSettings=SimpleNamespace)}
        with patch.dict(sys.modules, modules), patch.object(Path, 'mkdir'), \
             patch('app.ee_session._load_extra_cas') as cas, \
             patch.object(adapter.time, 'monotonic', return_value=100), \
             patch.dict(os.environ, EODAG_CONFIG_FILE=''):
            one = adapter._gateway(Path('downloads') / 'eodag', 'earth_search', 120)
            two = adapter._gateway(Path('downloads') / 'eodag', 'planetary_computer', 200)
            plugin = SimpleNamespace(config=SimpleNamespace(timeout=999, retry_total=3, ssl_verify=False,
                                     sort={'sort_param_mapping': {'start_datetime': 'properties.datetime'}}))
            one._do_search(plugin)
            self.assertEqual(plugin.config.timeout, 20)
            self.assertEqual(plugin.config.retry_total, 0)
            self.assertTrue(plugin.config.ssl_verify)
            self.assertIsNotNone(one.last_page)
            self.assertIsNone(two.last_page)
            self.assertEqual(one.settings.providers_whitelist, ['earth_search'])
            self.assertEqual(two.settings.providers_whitelist, ['planetary_computer'])
            self.assertEqual(one.settings.cfg_dir, Path('downloads') / 'eodag')
            self.assertEqual(cas.call_count, 2)
            with self.assertRaises(CatalogUnavailable):
                one.fetch_collections_list()
        self.assertEqual(adapter.normalize(product(platform='Not Available'), 'earth_search')['platform'], None)
        with self.assertRaises(CatalogFailure):
            adapter.normalize(product(**{'_collection': 'other'}), 'earth_search')

    def test_disabled_invalid_cap_allowlist_and_collection(self):
        with patch.object(adapter, '_gateway') as factory:
            with patch.dict(os.environ, EODAG_ENABLED='false'):
                with self.assertRaises(CatalogUnavailable):
                    self.search(Page())
            for value in ('0', '4001', 'nope'):
                with patch.dict(os.environ, EODAG_MAX_RESULTS=value), self.assertRaises(CatalogUnavailable):
                    self.search(Page())
            factory.assert_not_called()
        for provider, collection in ((None, None), ('other', None), ('earth_search', 'S2_MSI_L2A')):
            with self.assertRaises(ValueError):
                adapter.validate_selection(provider, collection)


if __name__ == '__main__':
    unittest.main()
