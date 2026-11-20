"""Opt-in real SDK/provider catalogue evidence; never fetches assets."""
import importlib.metadata
import json
import os
import unittest

from app.eodag_adapter import COLLECTIONS, search


@unittest.skipUnless(os.getenv('EODAG_SMOKE') == 'true', 'Set EODAG_SMOKE=true in an isolated optional environment')
class TestEodagSmoke(unittest.TestCase):
    def test_both_provider_catalogues(self):
        self.assertEqual(importlib.metadata.version('eodag'), '4.9.2')
        for provider in COLLECTIONS:
            with self.subTest(provider=provider):
                result = search((106.6, 10.7, 106.8, 10.9), '2026-01-01', '2026-01-31', provider=provider)
                print(json.dumps({'provider': provider, 'parameters': result.parameters,
                                  'total': len(result.scenes), 'sample': result.scenes[:1]}, sort_keys=True))
                self.assertTrue(result.scenes, 'Provider returned no usable catalogue scenes')
                self.assertEqual(result.parameters['completion'], 'complete')
                self.assertTrue(all(row['provider'] == provider for row in result.scenes))
                self.assertTrue(all(row['download_capabilities']['status'] == 'unsupported' for row in result.scenes))


if __name__ == '__main__':
    unittest.main()
