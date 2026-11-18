"""Exercise real EE expression construction at the metadata transport boundary."""
import json
from unittest import mock

import ee
from ee.apitestcase import ApiTestCase

from app import inventory_sources as sources, imagery, main
from app.geo import RectangleROI


class GEEMetadataInventoryTests(ApiTestCase):
    def query(self, **kwargs):
        geometry = RectangleROI(center_lon=105.58731973171236, center_lat=20.036475833269925,
            width_m=40.39367743961984, height_m=55.18933644607458).geometry
        return sources.list_scenes(dataset=kwargs.pop('dataset', 'sentinel2'), source='gee',
            geometry=geometry, start='2018-01-13', end='2024-12-31', **kwargs)

    def test_count_never_runs_raster_preparation_for_multiyear_roi(self):
        def compute(expression, deadline):
            graph = json.dumps(ee.serializer.encode(expression, for_cloud_api=True))
            self.assertIn(imagery.S2_COLLECTION, graph)
            self.assertIn('2025-01-01', graph)
            self.assertIn('Sentinel-2C', graph)
            for operation in ('Image.select', 'Image.multiply', 'Image.addBands', 'Image.copyProperties'):
                self.assertNotIn(operation, graph)
            self.assertIn('105.587', graph)
            return [{'asset_id':imagery.S2_COLLECTION + '/scene', 'timestamp_ms':1516333344303, 'cloud_pct':None}]
        with mock.patch.object(main, '_ensure_ee'), \
                mock.patch.object(imagery, 'timeseries_collection', side_effect=AssertionError('must not prepare raster bands')), \
                mock.patch.object(sources, '_compute_gee', side_effect=compute, create=True):
            rows = self.query(platform='C')
        self.assertEqual(rows[0]['date'], '2018-01-19')
        self.assertEqual(rows[0]['cloud_pct'], None)
        self.assertEqual(rows[0]['asset_id'], imagery.S2_COLLECTION + '/scene')

    def test_landsat_tags_native_collection_before_merge(self):
        with mock.patch.object(main, '_ensure_ee'), mock.patch.object(sources, '_compute_gee', return_value=[], create=True) as transport:
            self.query(dataset='landsat')
        graph = json.dumps(ee.serializer.encode(transport.call_args.args[0], for_cloud_api=True))
        self.assertIn(imagery.L8_COLLECTION, graph)
        self.assertIn(imagery.L9_COLLECTION, graph)
        self.assertIn('Collection.merge', graph)
        self.assertIn('system:index', graph)
        self.assertNotIn('Image.select', graph)

    def test_sar_platform_polarization_and_orbit_still_filter_native_collection(self):
        with mock.patch.object(main, '_ensure_ee'), mock.patch.object(sources, '_compute_gee', return_value=[], create=True) as transport:
            self.query(dataset='s1gee', platform='D', polarization='hhhv', orbit_pass='DESCENDING', relative_orbit=42)
        graph = json.dumps(ee.serializer.encode(transport.call_args.args[0], for_cloud_api=True))
        for value in ('platform_number', 'HH', 'HV', 'instrumentMode', 'DESCENDING', 'relativeOrbitNumber_start', '42'):
            self.assertIn(value, graph)
        self.assertNotIn('Image.clamp', graph)
