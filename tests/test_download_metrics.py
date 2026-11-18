import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class MetricsTests(unittest.TestCase):
    def metrics(self):
        from app.download_metrics import DownloadMetrics
        return DownloadMetrics('job-123')

    def test_nested_phases_record_exception_without_swallowing(self):
        metrics = self.metrics()
        with patch('app.download_metrics.time.perf_counter', side_effect=[1., 2., 4., 6.]):
            with self.assertRaisesRegex(ValueError, 'original'):
                with metrics.phase('assembly'):
                    with metrics.phase('warp'):
                        raise ValueError('original')
        self.assertEqual(metrics.report()['phases_s'], {'warp': 2., 'assembly': 5.})
        self.assertEqual(metrics.report()['failed_phases'], ['warp', 'assembly'])

    def test_reports_safe_json_csv_and_counts_bytes(self):
        metrics = self.metrics()
        metrics.record_tile('B2-01', event='verified', bytes_count=42, elapsed_s=.2, attempt=1)
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / 'report.json'
            metrics.write_report(target)
            report = json.loads(target.read_text())
            self.assertEqual(report['tiles'][0]['bytes_count'], 42)
            self.assertTrue(target.with_suffix('.csv').exists())
            self.assertGreater(report['resources']['rss_peak_bytes'], 0)

    def test_untrusted_labels_cannot_leak_url_token_or_exception(self):
        metrics = self.metrics()
        with self.assertRaises(ValueError):
            metrics.record_tile('https://example.test?token=secret', event='verified')
        with self.assertRaises(ValueError):
            with metrics.phase('Bearer secret'):
                pass
        self.assertNotIn('secret', json.dumps(metrics.report()))
