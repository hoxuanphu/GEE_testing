"""Numerical and EE graph regressions for overlap-based RAW calibration."""
import unittest
from unittest import mock

import ee
from ee.apitestcase import ApiTestCase
import numpy as np

from app import color_balance as cb


class FitTests(unittest.TestCase):
    def test_known_affine_and_sparse_outliers_reduce_held_out_error(self):
        source = np.linspace(.05, .45, 1000)
        target = source * 1.2 + .025
        target[::31] = .95
        fit = cb.fit_band(source, target)
        self.assertEqual(fit['status'], 'applied')
        self.assertAlmostEqual(fit['gain'], 1.2, delta=.04)
        self.assertAlmostEqual(fit['offset'], .025, delta=.01)
        held_out = np.linspace(.075, .425, 171)
        error = np.median(abs(held_out * fit['gain'] + fit['offset'] - (held_out * 1.2 + .025)))
        self.assertLess(error, .005)

    def test_flat_small_nonfinite_extreme_and_unrelated_samples_are_rejected(self):
        x = np.linspace(.1, .4, 200)
        for source, target, reason in [
            (x[:20], x[:20], 'insufficient_samples'),
            (np.full(200, .1), np.full(200, .2), 'flat_overlap'),
            (x, x * 3, 'unsafe_coefficients'),
            (x, x + .2, 'unsafe_coefficients'),
            (x, np.random.default_rng(9).permutation(x), 'poor_fit'),
            (np.full(100, np.nan), np.full(100, np.inf), 'insufficient_samples'),
        ]:
            with self.subTest(reason=reason):
                fit = cb.fit_band(source, target)
                self.assertEqual(fit['status'], 'skipped')
                self.assertEqual(fit['reason'], reason)
                self.assertEqual((fit['gain'], fit['offset']), (1., 0.))

    def test_already_aligned_is_identity_even_for_flat_overlap(self):
        fit = cb.fit_band(np.full(100, .2), np.full(100, .201))
        self.assertEqual(fit['status'], 'identity')
        self.assertEqual((fit['gain'], fit['offset']), (1., 0.))

    def test_nodata_pairs_are_removed_together(self):
        x = np.linspace(.05, .45, 200)
        y = x * .8 + .02
        x[::7] = np.nan
        y[::11] = np.inf
        fit = cb.fit_band(x, y)
        self.assertEqual(fit['status'], 'applied')
        self.assertAlmostEqual(fit['gain'], .8)
        self.assertAlmostEqual(fit['offset'], .02)


class GraphTests(ApiTestCase):
    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, '_install_cloud_api_resource', install)

    def image(self, value=.2):
        return ee.Image.constant([value] * 12 + [4, 7]).rename(cb.S2_BANDS + ['SCL', 'QA60'])

    def test_apply_overwrites_only_spectral_bands_without_filling_or_cloud_masking(self):
        image = self.image()
        graph = cb.apply_coefficients(image, {'B2': {'status': 'applied', 'gain': 1.2, 'offset': .02}}).serialize()
        self.assertIn('Image.addBands', graph)
        self.assertIn('Image.multiply', graph)
        self.assertNotIn('Image.unmask', graph)
        self.assertNotIn('Image.updateMask', graph)
        self.assertNotIn('Image.clamp', graph)

    def test_disconnected_scene_is_unchanged_and_marked_skipped(self):
        images = [self.image(), self.image(.3)]
        evaluate = mock.Mock(return_value={'features': []})
        result, report = cb.balance_sources(images, 'sentinel2', ee.Geometry.Rectangle([106, 10, 106.1, 10.1]),
                                             evaluate, ['a', 'b'])
        self.assertIs(result[0], images[0])
        self.assertIs(result[1], images[1])
        self.assertEqual(report['scenes'][1]['reason'], 'insufficient_overlap')
        self.assertLessEqual(evaluate.call_count, cb.MAX_SAMPLE_CALLS)

    def test_chain_retries_failed_overlap_after_a_new_reference_is_available(self):
        bands = cb.S2_BANDS
        def samples(reference):
            return {'features': [{'properties': {
                **{f's_{band}': v for band in bands},
                **{f'r_{band}': v * 1.2 + .02 for band in bands},
                'reference_index': reference}}
                for v in np.linspace(.05, .45, 100)]}
        evaluate = mock.Mock(side_effect=[{'features': []}, samples(0), samples(2)])
        result, report = cb.balance_sources([self.image()] * 3, 'sentinel2',
            ee.Geometry.Rectangle([106, 10, 106.1, 10.1]), evaluate, ['a', 'c', 'b'])
        self.assertEqual(report['scenes'][1]['status'], 'applied')
        self.assertEqual(report['scenes'][1]['reference_scene_id'], 'b')
        self.assertEqual(report['scenes'][2]['reference_scene_id'], 'a')
        self.assertEqual(evaluate.call_count, 3)
        self.assertEqual(len(result), 3)

    def test_budget_exhaustion_is_reported(self):
        evaluate = mock.Mock(return_value={'features': []})
        with mock.patch.object(cb, 'MAX_SAMPLE_CALLS', 1):
            _, report = cb.balance_sources([self.image()] * 3, 'sentinel2',
                ee.Geometry.Rectangle([106, 10, 106.1, 10.1]), evaluate, ['a', 'b', 'c'])
        self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(report['scenes'][2]['reason'], 'sampling_budget_exhausted')


if __name__ == '__main__':
    unittest.main()
