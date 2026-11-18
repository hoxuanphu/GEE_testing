"""Group preview source order, common masks and bounded display transport."""
import json
import time
from unittest import mock

import ee
import numpy as np
from ee.apitestcase import ApiTestCase

from app import frame_search as search
from app.frame_models import FrameError
from app.reference_frame import reference_roi_identity


FRAME = {'crs': 'EPSG:32648', 'transform': [10, 0, 398000, 0, -10, 2400000],
         'width': 8, 'height': 5}
PNG = b'\x89PNG\r\n\x1a\npreview'


def parameters(dataset='sentinel2', bands=None, frame=None):
    frame = frame or FRAME
    return {'dataset': dataset, 'bands': bands or ['B4', 'B3', 'B2', 'B8'],
            'reference': frame, 'output_grid': frame, **reference_roi_identity(frame)}


def sources(dataset='sentinel2'):
    return [{'asset_id': search.COLLECTIONS[dataset] + '/' + name} for name in ('first', 'second')]


def evaluate_pixels(image, assets):
    """Evaluate the actual serialized graph on four pixels, without EE/network.

    Spatial nodes are identities here; graph tests separately verify the grid.
    Only source fixtures are substituted, so source and mask operations remain
    the exact operations constructed by production code.
    """
    graph = json.loads(image.serialize())

    def evaluate(node):
        if 'valueReference' in node:
            return evaluate(graph['values'][node['valueReference']])
        if 'constantValue' in node:
            return node['constantValue']
        if 'arrayValue' in node:
            return [evaluate(value) for value in node['arrayValue']['values']]
        call = node['functionInvocationValue']
        name = call['functionName']
        args = {key: evaluate(value) for key, value in call.get('arguments', {}).items()}
        if name == 'Image.load':
            return assets[args['id']]
        if name == 'Image.constant':
            return {'constant': np.ma.asarray(args['value'])}
        if name == 'Projection':
            return args['crs']
        if name == 'Reducer.min':
            return 'min'
        if name == 'Image.select':
            return {band: args['input'][band] for band in args['bandSelectors']}
        if name in ('Image.toFloat', 'Image.toByte'):
            return args['value']
        if name == 'Image.reproject':
            return args['image']
        if name == 'Image.addBands':
            return {**args['dstImg'], **args['srcImg']}
        if name == 'Image.rename':
            return dict(zip(args['names'], args['input'].values()))
        if name == 'Image.mask':
            return {band: np.ma.asarray(~np.ma.getmaskarray(value))
                    for band, value in args['image'].items()}
        if name in ('Image.gt', 'Image.multiply'):
            left, right = args['image1'], args['image2']
            def other(band):
                return right[band] if band in right else next(iter(right.values()))
            return {band: value > other(band) if name == 'Image.gt' else value * other(band)
                    for band, value in left.items()}
        if name == 'Image.reduce':
            assert args['reducer'] == 'min'
            return {'min': np.ma.min(np.ma.stack(list(args['image'].values())), axis=0)}
        if name == 'Image.updateMask':
            common = next(iter(args['mask'].values())).filled(0).astype(bool)
            return {band: np.ma.array(value, mask=np.ma.getmaskarray(value) | ~common)
                    for band, value in args['image'].items()}
        if name == 'Image.unmask':
            source, filler = args['input'], args['value']
            return {band: np.ma.where(np.ma.getmaskarray(value),
                                     filler.get(band, next(iter(filler.values()))), value)
                    for band, value in source.items()}
        raise AssertionError('Unsupported graph operation: ' + name)

    return evaluate(graph['values'][graph['result']])


class SelectionPreviewGraphTests(ApiTestCase):
    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, '_install_cloud_api_resource', install)

    def helper(self):
        self.assertTrue(hasattr(search, '_frame_selection_image'),
                        'Group preview graph helper is not implemented')
        return search._frame_selection_image

    def test_all_export_band_masks_share_grid_before_intersection(self):
        image = self.helper()(sources(), parameters())
        encoded = image.serialize()
        self.assertIn('B8', encoded)  # Non-display band must affect source selection.
        self.assertIn('Image.mask', encoded)
        self.assertIn('Image.reduce', encoded)
        self.assertIn('Reducer.min', encoded)
        self.assertIn('Image.reproject', encoded)
        self.assertIn('EPSG:32648', encoded)
        self.assertNotIn('ImageCollection.mosaic', encoded)
        self.assertNotIn('SCL', encoded)  # Clouds remain valid source data.

    def test_first_source_wins_and_later_sources_only_fill_whole_masked_vectors(self):
        helper = self.helper()
        first = helper(sources(), parameters())
        second = helper(list(reversed(sources())), parameters())
        # unmask(input=primary, value=filler) defines first-source priority explicitly.
        self.assertEqual(first.func.getSignature()['name'], 'Image.unmask')
        self.assertIn('/first', first.args['input'].serialize())
        self.assertNotIn('/second', first.args['input'].serialize())
        self.assertIn('/second', first.args['value'].serialize())
        self.assertIn('/second', second.args['input'].serialize())
        self.assertEqual(first.args['input'].func.getSignature()['name'], 'Image.updateMask')
        mask = first.args['input'].args['mask'].serialize()
        for band in parameters()['bands']:
            self.assertIn(band, mask)
        self.assertFalse(first.args['sameFootprint'])

    def test_colored_sources_switch_only_overlap_and_non_display_holes_fill_as_whole_vectors(self):
        first = {band: np.ma.array([0, 1000 + offset, 2000 + offset, 3000 + offset],
                                   mask=[False, False, band == 'B8', True])
                 for band, offset in zip(parameters()['bands'], [0, 100, 200, 300])}
        second = {band: np.ma.array([4000 + offset] * 4, mask=[True, False, False, True])
                  for band, offset in zip(parameters()['bands'], [0, 100, 200, 300])}
        assets = dict(zip([source['asset_id'] for source in sources()], [first, second]))
        forward = evaluate_pixels(self.helper()(sources(), parameters()), assets)
        reverse = evaluate_pixels(self.helper()(list(reversed(sources())), parameters()), assets)
        for band, offset in zip(['B4', 'B3', 'B2'], [0, 100, 200]):
            with self.subTest(band=band):
                np.testing.assert_allclose(forward[band][:3], [0, (1000 + offset)*.0001,
                                                              (4000 + offset)*.0001])
                np.testing.assert_allclose(reverse[band][:3], [0, (4000 + offset)*.0001,
                                                              (4000 + offset)*.0001])
                self.assertTrue(np.ma.getmaskarray(forward[band])[3])
                self.assertTrue(np.ma.getmaskarray(reverse[band])[3])

    def test_custom_eqc_uses_shared_standard_transport_and_wgs84_roi(self):
        from app.frame_projection import plan_transport_grid
        frame = {'crs': '+proj=eqc +lat_ts=21.7423783969593 +lat_0=21.7423783969593 '
                        '+lon_0=104.035720835673 +R=6378166.17539632 +units=m',
                 'transform': [10, 0, -8065, 0, -10, 8095], 'width': 1614, 'height': 1619}
        params = parameters(frame=frame)
        expected = plan_transport_grid(frame, params['dataset'], params['bands'])['grid']
        rpc = mock.Mock()
        rpc.thumbnail_once.return_value = PNG
        self.assertTrue(hasattr(search, 'make_frame_selection_preview'))
        with mock.patch.object(search, '_RPC') as scoped:
            scoped.return_value.__enter__.return_value = rpc
            search.make_frame_selection_preview(sources(), params)
        image, options = rpc.thumbnail_once.call_args.args
        self.assertIn(expected['crs'], image.serialize())
        self.assertNotIn('+proj=eqc', image.serialize())
        self.assertNotIn('Equirectangular', image.serialize())
        self.assertEqual(options['crs'], expected['crs'])
        self.assertIn('EPSG:4326', options['region'].serialize())
        self.assertIn('21.7', options['region'].serialize())

    def test_png_512_display_scales_s2_once_and_deadline_is_120_total(self):
        self.assertTrue(hasattr(search, 'make_frame_selection_preview'))
        rpc = mock.Mock()
        rpc.thumbnail_once.return_value = PNG
        before = time.monotonic()
        with mock.patch.object(search, '_RPC') as scoped:
            scoped.return_value.__enter__.return_value = rpc
            self.assertEqual(search.make_frame_selection_preview(sources(), parameters()), PNG)
        args, kwargs = rpc.thumbnail_once.call_args
        options = args[1]
        self.assertEqual(options['dimensions'], 512)
        self.assertEqual(options['bands'], ['B4', 'B3', 'B2'])
        self.assertEqual((options['min'], options['max']), (0, .3))
        self.assertTrue(kwargs['preview'])
        self.assertLessEqual(kwargs['deadline'], before + 120.1)
        self.assertEqual(scoped.call_args.args[0], 120)
        primary = args[0].args['input']
        graph = json.loads(primary.serialize())
        def multiplication_count(node):
            if isinstance(node, list):
                return sum(multiplication_count(value) for value in node)
            if not isinstance(node, dict):
                return 0
            return int(node.get('functionName') == 'Image.multiply') + sum(
                multiplication_count(value) for value in node.values())
        self.assertEqual(multiplication_count(graph), 1)
        self.assertIn('0.0001', primary.serialize())

    def test_sar_displays_selected_hh_and_preserves_db(self):
        self.assertTrue(hasattr(search, 'make_frame_selection_preview'))
        rpc = mock.Mock()
        rpc.thumbnail_once.return_value = PNG
        with mock.patch.object(search, '_RPC') as scoped:
            scoped.return_value.__enter__.return_value = rpc
            search.make_frame_selection_preview(sources('s1gee'), parameters('s1gee', ['HH', 'HV']))
        image, options = rpc.thumbnail_once.call_args.args
        self.assertEqual(options['bands'], ['HH'])
        self.assertEqual((options['min'], options['max']), (-25, 0))
        self.assertIn('HV', image.serialize())
        self.assertNotIn('Image.multiply', image.serialize())

    def test_stale_roi_and_invalid_selection_stop_before_rpc(self):
        self.assertTrue(hasattr(search, 'make_frame_selection_preview'))
        invalid = parameters()
        invalid.pop('roi_version')
        for scenes, params in ((sources(), invalid), ([], parameters()), (sources() * 6, parameters())):
            with mock.patch.object(search, '_RPC') as rpc, self.assertRaises(FrameError):
                search.make_frame_selection_preview(scenes, params)
            rpc.assert_not_called()
