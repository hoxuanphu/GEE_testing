import unittest
from unittest import mock
import ee
from ee.apitestcase import ApiTestCase
from shapely.geometry import box, mapping

from app import scene_grouping as grouping


DAY = 86400000
ROI = mapping(box(0, 0, 2, 1))


def row(name, day, tile, footprint, **properties):
    return {'system:index': name, 'system:time_start': day * DAY,
            'SPACECRAFT_NAME': 'Sentinel-2A', 'MGRS_TILE': tile,
            'DATATAKE_IDENTIFIER': f'pass-{day}', 'SENSING_ORBIT_NUMBER': 75,
            'MEAN_SOLAR_ZENITH_ANGLE': 35, 'MEAN_SOLAR_AZIMUTH_ANGLE': 140,
            'MEAN_INCIDENCE_ZENITH_ANGLE_B4': 5, 'cloud': 10,
            'footprint': mapping(footprint), **properties}


class GroupingTests(unittest.TestCase):
    def test_full_same_pass_beats_mixed_days_and_shared_orbit_does_not_mean_same_pass(self):
        rows = [row('a', 1, 'left', box(0, 0, 1, 1)),
                row('b', 1, 'right', box(1, 0, 2, 1)),
                row('c', 3, 'right', box(1, 0, 2, 1), cloud=0)]
        groups = grouping.rank_groups(rows, 'sentinel2', 'near_date', 3, ROI)
        self.assertEqual(set(groups[0]['indices']), {0, 1})
        self.assertTrue(groups[0]['same_pass'])
        self.assertNotEqual(grouping.acquisition_key(rows[0], 'sentinel2'), grouping.acquisition_key(rows[2], 'sentinel2'))

    def test_span_is_whole_group_not_chained_gaps(self):
        rows = [row(str(i), day, str(i), box(i / 3, 0, (i + 1) / 3, 1))
                for i, day in enumerate([0, 3, 6])]
        groups = grouping.rank_groups(rows, 'sentinel2', 'near_date', 3, ROI)
        self.assertTrue(groups)
        for group in groups:
            self.assertLessEqual(group['span_days'], 3)
            self.assertNotEqual(set(group['indices']), {0, 1, 2})

    def test_missing_pass_or_time_is_not_silently_grouped(self):
        a = row('a', 1, 'left', box(0, 0, 1, 1), DATATAKE_IDENTIFIER=None)
        b = row('b', 1, 'right', box(1, 0, 2, 1), DATATAKE_IDENTIFIER=None)
        groups = grouping.rank_groups([a, b], 'sentinel2', 'same_pass', 3, ROI)
        self.assertTrue(all(len(group['indices']) == 1 for group in groups))
        b['system:time_start'] = None
        groups = grouping.rank_groups([a, b], 'sentinel2', 'near_date', 3, ROI)
        self.assertFalse(any(1 in group['indices'] for group in groups))

    def test_union_coverage_is_not_sum_of_overlapping_footprints(self):
        rows = [row('a', 1, 'a', box(0, 0, 1, 1)), row('b', 1, 'b', box(0, 0, 1, 1))]
        group = grouping.rank_groups(rows, 'sentinel2', 'same_pass', 3, ROI)[0]
        self.assertAlmostEqual(group['footprint_coverage_pct'], 50.)

    def test_same_pass_allows_adjacent_tiles_with_seconds_difference(self):
        a = row('a', 1, 'left', box(0, 0, 1, 1))
        b = row('b', 1, 'right', box(1, 0, 2, 1), **{'system:time_start': DAY + 15000})
        group = grouping.rank_groups([a, b], 'sentinel2', 'same_pass', 3, ROI)[0]
        self.assertEqual(set(group['indices']), {0, 1})

    def test_landsat_pass_keeps_platform_and_path_but_allows_rows(self):
        a = {'SPACECRAFT_ID': 'LANDSAT_8', 'WRS_PATH': 125, 'WRS_ROW': 53, 'system:time_start': DAY}
        b = {**a, 'WRS_ROW': 54, 'system:time_start': DAY + 20000}
        c = {**b, 'SPACECRAFT_ID': 'LANDSAT_9'}
        self.assertEqual(grouping.acquisition_key(a, 'landsat'), grouping.acquisition_key(b, 'landsat'))
        self.assertNotEqual(grouping.acquisition_key(a, 'landsat'), grouping.acquisition_key(c, 'landsat'))

    def test_angles_wrap_and_missing_metadata_is_penalized(self):
        self.assertEqual(grouping.angle_distance(359, 1), 2)
        a = row('a', 1, 'a', box(0, 0, 1, 1))
        b = {**a, 'MEAN_SOLAR_ZENITH_ANGLE': None}
        self.assertGreater(grouping.condition_distance(a, b, 'sentinel2'), grouping.condition_distance(a, a, 'sentinel2'))

    def test_limits_are_explicit_and_results_deterministic(self):
        rows = [row(str(i), i, str(i), box(0, 0, 2, 1)) for i in range(30)]
        self.assertLessEqual(len(grouping.rank_groups(rows, 'sentinel2', 'near_date', 3, ROI)), grouping.MAX_GROUPS)
        with self.assertRaisesRegex(ValueError, '400'):
            grouping.rank_groups(rows * 14, 'sentinel2', 'near_date', 3, ROI)


class GroupedBuilderTests(ApiTestCase):
    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, '_install_cloud_api_resource', install)

    def test_pixel_coverage_failure_never_broadens_dates(self):
        from app import imagery
        rows = [row('a', 1, 'a', box(0, 0, 1, 1)), row('b', 4, 'b', box(1, 0, 2, 1))]
        image = ee.Image.constant([.2] * 6).rename(['B2', 'B3', 'B4', 'B8', 'B11', 'B12'])
        picked = [{'scene_id': 'a', 'date': '1970-01-02', 'cloud': 10}]
        with mock.patch.object(imagery, '_group_metadata', return_value=(rows, ROI)), \
             mock.patch.object(imagery, '_least_cloudy_per_tile', return_value=(image, picked)) as select, \
             mock.patch.object(imagery, '_coverage_fraction', return_value=mock.Mock(getInfo=lambda: .5)):
            with self.assertRaisesRegex(imagery.InsufficientCoverageError, '50.00'):
                imagery._coherent_optical_scene(ee.ImageCollection([]), 'sentinel2', 'cloud',
                    ee.Geometry(ROI), 'start', 'end', 'raw', 'near_date', 1)
        self.assertLessEqual(select.call_count, grouping.MAX_PIXEL_CHECKS)

    def test_report_retains_timestamp_acquisition_and_output_cloud_policy(self):
        from app import imagery
        rows = [row('a', 1, 'a', box(0, 0, 2, 1))]
        image = ee.Image.constant([.2] * 6).rename(['B2', 'B3', 'B4', 'B8', 'B11', 'B12'])
        picked = [{'scene_id': 'a', 'date': '1970-01-02', 'cloud': 10}]
        with mock.patch.object(imagery, '_group_metadata', return_value=(rows, ROI)), \
             mock.patch.object(imagery, '_least_cloudy_per_tile', return_value=(image, picked)), \
             mock.patch.object(imagery, '_coverage_fraction', return_value=mock.Mock(getInfo=lambda: 1.)):
            result = imagery._coherent_optical_scene(ee.ImageCollection([]), 'sentinel2', 'cloud',
                ee.Geometry(ROI), 'start', 'end', 'mosaic', 'same_pass', 3, mosaic_mask_clouds=False)
        self.assertFalse(result.mosaic_mask_clouds)
        self.assertEqual(result.scene_grouping['span_days'], 0.)
        self.assertEqual(result.scenes[0]['DATATAKE_IDENTIFIER'], 'pass-1')
        self.assertEqual(result.scenes[0]['timestamp'], DAY)

    def test_stable_unmasked_graph_is_bound_for_durable_download(self):
        from app import imagery
        from app.download_snapshots import freeze_selection
        rows = [row('a', 1, 'a', box(0, 0, 2, 1))]
        native = ee.Image(imagery.S2_COLLECTION + '/a')
        coll = ee.ImageCollection.fromImages([native.set(rows[0])])
        with mock.patch.object(imagery, '_group_metadata', return_value=(rows, ROI)), \
             mock.patch.object(imagery, '_haze_rank', return_value=coll), \
             mock.patch.object(imagery, 'load_timeseries_scene', return_value=native), \
             mock.patch.object(ee.List, 'getInfo', return_value=['a']), \
             mock.patch.object(ee.Image, 'getInfo', return_value={'properties': rows[0]}), \
             mock.patch.object(imagery, '_coverage_fraction', return_value=mock.Mock(getInfo=lambda: 1.)):
            result = imagery._coherent_optical_scene(coll, 'sentinel2', 'cloud', ee.Geometry(ROI),
                'start', 'end', 'mosaic', 'same_pass', 3, mosaic_mask_clouds=False)
        frozen = freeze_selection(result, {}, provider_scope='test')
        self.assertNotIn('Collection.first', frozen.image_graph)

    def test_missing_angles_are_reported_even_with_complete_pass_identifier(self):
        from app import imagery
        rows = [row('a', 1, 'a', box(0, 0, 2, 1), MEAN_SOLAR_ZENITH_ANGLE=None)]
        image = ee.Image.constant([.2] * 6).rename(['B2','B3','B4','B8','B11','B12'])
        with mock.patch.object(imagery, '_group_metadata', return_value=(rows, ROI)), \
             mock.patch.object(imagery, '_least_cloudy_per_tile', return_value=(image, [{'scene_id':'a','date':'1970-01-02','cloud':10}])), \
             mock.patch.object(imagery, '_coverage_fraction', return_value=mock.Mock(getInfo=lambda: 1.)):
            result = imagery._coherent_optical_scene(ee.ImageCollection([]), 'sentinel2', 'cloud',
                ee.Geometry(ROI), 'start', 'end', 'mosaic', 'same_pass', 3, mosaic_mask_clouds=False)
        self.assertIn('a', result.scene_grouping['missing_metadata_scenes'])

    def test_landsat_mosaic_loader_keeps_native_qa_after_spectral_export_selection(self):
        from app import imagery
        spectral = ee.Image.constant([.2] * 6).rename([f'SR_B{i}' for i in range(2, 8)])
        with mock.patch.object(imagery, 'load_timeseries_scene', return_value=spectral):
            result = imagery._load_mosaic_asset(imagery.L8_COLLECTION + '/LC08_125053_20260101', 'landsat')
        self.assertIn('QA_PIXEL', result.serialize())


if __name__ == '__main__':
    unittest.main()
