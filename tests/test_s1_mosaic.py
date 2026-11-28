"""SAR fill mosaics keep the primary pixels and orbit geometry."""
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from app import main, s1_mosaic
from app.geo import Bbox
from app.imagery import SceneInfo


def scene():
    return SceneInfo('s1gee', 'COPERNICUS/S1_GRD', 'primary', '2026-05-20', -1, MagicMock())


class MosaicTests(unittest.TestCase):
    def setUp(self):
        reducer = patch.object(s1_mosaic.ee, 'Reducer')
        reducer.start()
        self.addCleanup(reducer.stop)

    def test_disabled_or_complete_does_not_search_candidates(self):
        for enabled, coverage in [(False, 45), (True, 100)]:
            with self.subTest(enabled=enabled), patch.object(s1_mosaic, 'coverage_pct', return_value=coverage), patch.object(s1_mosaic.ee, 'ImageCollection') as collection:
                result = s1_mosaic.prepare_s1(scene(), object(), '2026-05-01', '2026-06-01', 'vvvh', enabled)
                self.assertEqual(result.scene_count, 1)
                self.assertEqual(result.coverage_pct, coverage)
                collection.assert_not_called()

    def test_fill_keeps_primary_and_filters_orbit_and_all_bands(self):
        base = scene()
        raw = MagicMock()
        raw.toDictionary.return_value.getInfo.return_value = {
            'orbitProperties_pass': 'DESCENDING', 'relativeOrbitNumber_start': 18,
            'system:time_start': 1779235200000,
        }
        candidate = raw.select.return_value.clamp.return_value.float.return_value.updateMask.return_value
        collection = MagicMock()
        collection.filterBounds.return_value = collection
        collection.filterDate.return_value = collection
        collection.filter.return_value = collection
        collection.sort.return_value = collection
        collection.map.return_value = collection
        collection.limit.return_value = collection
        metadata = {'system:index': ['fill1', 'fill2'],
                    'system:time_start': [1779235200000, 1779148800000],
                    'platform_number': ['A', 'A']}
        collection.aggregate_array.side_effect = lambda key: MagicMock(getInfo=MagicMock(return_value=metadata[key]))
        with patch.object(s1_mosaic, 'coverage_pct', side_effect=[60, 60, 95]), patch.object(s1_mosaic.ee, 'Image', return_value=raw), patch.object(s1_mosaic.ee, 'ImageCollection', return_value=collection), patch.object(s1_mosaic.ee, 'Filter') as filters:
            result = s1_mosaic.prepare_s1(base, 'roi', '2026-05-01', '2026-06-01', 'vvvh', True)
        self.assertEqual(result.scene_count, 2)
        self.assertEqual(result.coverage_pct, 95)
        self.assertEqual([row['scene_id'] for row in result.scenes], ['primary', 'fill2'])
        self.assertTrue(result.scene_id.startswith('s1gee_mosaic_'))
        filters.eq.assert_any_call('orbitProperties_pass', 'DESCENDING')
        filters.eq.assert_any_call('relativeOrbitNumber_start', 18)
        filters.listContains.assert_any_call('transmitterReceiverPolarisation', 'VV')
        filters.listContains.assert_any_call('transmitterReceiverPolarisation', 'VH')
        primary_image = base.ee_object.select.return_value.updateMask.return_value
        primary_image.unmask.assert_called_with(candidate, sameFootprint=False)
        collection.limit.assert_called_once_with(24)

    def test_missing_orbit_metadata_refuses_unsafe_merge(self):
        raw = MagicMock()
        raw.toDictionary.return_value.getInfo.return_value = {}
        with patch.object(s1_mosaic, 'coverage_pct', return_value=50), patch.object(s1_mosaic.ee, 'Image', return_value=raw):
            with self.assertRaisesRegex(ValueError, 'quỹ đạo'):
                s1_mosaic.prepare_s1(scene(), 'roi', '2026-05-01', '2026-06-01', 'vvvh', True)

    def test_coverage_counts_uncovered_area_as_zero(self):
        image = MagicMock()
        valid = image.mask.return_value.reduce.return_value.rename.return_value
        valid.unmask.return_value.gt.return_value.reduceRegion.return_value.get.return_value.getInfo.return_value = .45
        with patch.object(s1_mosaic.ee, 'Reducer'):
            self.assertEqual(s1_mosaic.coverage_pct(image, 'roi'), 45)
        valid.unmask.assert_called_once_with(0, sameFootprint=False)

    def test_restore_rejects_a_scene_in_bbox_but_outside_province(self):
        from shapely.geometry import box, mapping

        collection = MagicMock()
        collection.filterBounds.return_value = collection
        collection.filter.return_value = collection
        collection.toList.return_value.map.return_value.getInfo.return_value = [{
            'system:index': 'outside',
            'relativeOrbitNumber_start': 55,
            'footprint': mapping(box(108, 10, 109, 11)),
        }]
        with patch.object(s1_mosaic, '_s1_collection', return_value=collection), \
             patch.object(s1_mosaic.ee, 'Filter'):
            with self.assertRaisesRegex(ValueError, 'không giao'):
                s1_mosaic.restore_s1_sources(['outside'], 'bbox-roi', '2026-01-01',
                                            '2026-01-02', 'vvvh', None, 'ASCENDING',
                                            province_geometry=mapping(box(106, 10, 107, 11)))
        collection.filterBounds.assert_called_once_with('bbox-roi')


class MosaicAPITests(unittest.TestCase):
    def test_province_download_checks_sources_with_bbox_and_clips_locally(self):
        from dataclasses import replace
        from tempfile import TemporaryDirectory
        from shapely.geometry import box, mapping
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        bounds = Bbox(106, 10, 107, 11)
        geometry = mapping(box(106, 10, 107, 11))
        province = main.Province('VN22', 'Test', geometry, bounds)
        sources = tuple({'scene_id': name, 'footprint': geometry} for name in ('primary', 'fill'))
        result = replace(scene(), scene_count=2, scenes=sources)

        def export(image, path, **options):
            with rasterio.open(path, 'w', driver='GTiff', width=1, height=1, count=2,
                               dtype='float32', crs='EPSG:4326',
                               transform=from_origin(106, 11, .1, .1)) as output:
                output.write(np.array([[[-19.0]], [[-22.0]]], dtype='float32'))

        with TemporaryDirectory() as root, patch.object(main, '_DOWNLOADS_DIR', Path(root)), \
             patch.object(main, '_ensure_ee'), patch.object(main, 'bbox_to_ee_geom', return_value='bbox-roi'), \
             patch.object(main, '_resolve_selection', return_value=('exact-roi', bounds, province)), \
             patch.object(s1_mosaic, 'restore_s1_sources', return_value=result) as restore, \
             patch.object(main, 'download_geotiff', return_value=[]) as legacy, \
             patch('app.full_scene_export.export_s1_province', create=True, side_effect=export) as clipped:
            response = main.s1gee_download('2023-05-01', '2023-08-31', selection_mode='province',
                                           province_id='VN22', orbit_pass='ASCENDING',
                                           scene_ids=['primary','fill'], merge_scenes=True)
            self.assertEqual(restore.call_args.args[1], 'bbox-roi')
            self.assertEqual(restore.call_args.kwargs['province_geometry'], geometry)
            self.assertEqual(clipped.call_args.kwargs['province_geometry'], geometry)
            self.assertEqual(clipped.call_args.kwargs['bands'], ['VV', 'VH'])
            legacy.assert_not_called()
            with rasterio.open(response.path) as raster:
                self.assertEqual(json.loads(raster.tags()['SAR_SOURCES']),
                                 [{'scene_id': 'primary'}, {'scene_id': 'fill'}])
                self.assertEqual(raster.tags()['EXPORT_EXTENT'], 'province')

    def test_province_listing_uses_light_search_and_thumbnail_geometries(self):
        from dataclasses import replace
        exact = {'type': 'Polygon', 'coordinates': [[[106, 10], [107, 10], [107, 11], [106, 10]]]}
        province = main.Province('test', 'Test', exact, Bbox(106, 10, 107, 11))
        choice = replace(scene(), scene_count=1, scenes=({'scene_id': 'primary'},))
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee'), \
             patch.object(main, '_resolve_selection', return_value=('exact-roi', province.bbox, province)), \
             patch.object(s1_mosaic, 'province_search_roi', return_value='search-roi', create=True), \
             patch.object(s1_mosaic, 'list_s1_results', return_value=[('ASCENDING', choice)]) as search, \
             patch.object(main, '_component_thumbnail_links', wraps=main._component_thumbnail_links) as previews:
            response = client.post('/api/s1gee/scene', json={'selection_mode': 'province', 'province_id': 'test',
                                   'start_date': '2023-05-01', 'end_date': '2023-08-31', 'list_scenes': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(search.call_args.args[:3], ('search-roi', '2023-05-01', '2023-09-01'))
        self.assertEqual(response.json()['roi_geometry'], exact)
        self.assertEqual(len(previews.call_args.args[2]['coordinates'][0]), 5)
        self.assertIn('province_id=test', response.json()['results'][0]['download_url'])

    def test_listing_keeps_multiple_scenes_in_one_orbit_with_footprints_and_pinned_links(self):
        from dataclasses import replace
        footprint = {'type': 'Polygon', 'coordinates': [[[106, 10], [107, 10], [107, 11], [106, 10]]]}
        choices = [replace(scene(), scene_id=value, scene_count=1,
                           scenes=({'scene_id': value, 'footprint': footprint},))
                   for value in ('first', 'second')]
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee'), \
             patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), None)), \
             patch.object(s1_mosaic, 'list_s1_results', return_value=[('ASCENDING', choice) for choice in choices]):
            response = client.post('/api/s1gee/scene', json=dict(lon=107, lat=11,
                                   start_date='2026-05-01', end_date='2026-05-31', list_scenes=True))
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()['results']
        self.assertEqual([item['scene_id'] for item in items], ['first', 'second'])
        for item in items:
            self.assertEqual(item['footprint'], footprint)
            query = parse_qs(urlsplit(item['download_url']).query)
            self.assertEqual(query['scene_ids'], [item['scene_id']])
            full = parse_qs(urlsplit(item['full_scene_download_url']).query)
            self.assertEqual(full['scene_ids'], [item['scene_id']])
            self.assertEqual(full['extent'], ['scene'])
            self.assertEqual(full['orbit_pass'], ['ASCENDING'])
            self.assertEqual(full['polarization'], ['vvvh'])
            self.assertNotIn('selection_mode', full)
        self.assertNotEqual(items[0]['preview_url'], items[1]['preview_url'])

    def test_search_exposes_each_orbit_and_pins_its_download_sources(self):
        from dataclasses import replace
        up = replace(scene(), scene_id='up', coverage_pct=99.9, scenes=({'scene_id': 'up'}, {'scene_id': 'fill'}))
        down = replace(scene(), scene_id='down', coverage_pct=76, scenes=({'scene_id': 'down'},))
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee'), \
             patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), None)), \
             patch.object(main, 'best_s1_scene', return_value=up), \
             patch.object(s1_mosaic, 'search_s1_by_orbit', return_value={'ASCENDING': up, 'DESCENDING': down}), \
             patch.object(main, 'preview_rgb', return_value=None), patch.object(s1_mosaic, 'prepare_s1', return_value=up):
            response = client.post('/api/s1gee/scene', json=dict(lon=107, lat=11, start_date='2026-05-01',
                                   end_date='2026-05-31', group_by_orbit=True, merge_scenes=True))
        self.assertEqual(response.status_code, 200, response.text)
        results = response.json()['results']
        self.assertEqual([item['orbit_pass'] for item in results], ['ASCENDING', 'DESCENDING'])
        for item, expected in zip(results, (['up', 'fill'], ['down'])):
            query = parse_qs(urlsplit(item['download_url']).query)
            self.assertEqual(query['scene_ids'], expected)
            self.assertEqual(query['orbit_pass'], [item['orbit_pass']])
        self.assertNotIn('full_scene_download_url', results[0])
        self.assertIn('full_scene_download_url', results[1])

    def test_full_scene_download_uses_exact_native_grid_and_selected_polarization(self):
        import numpy as np
        from affine import Affine
        from rasterio.crs import CRS
        from rasterio.io import MemoryFile

        from app import full_scene_export, imagery

        grid = Affine(10, 0, 402422.781, 0, -10, 1265876.076)
        image = MagicMock()
        footprint = {'type': 'Polygon', 'coordinates': [[[106, 10], [107, 10], [107, 11], [106, 10]]]}
        source = {'scene_id': 'primary', 'asset_id': 'COPERNICUS/S1_GRD/primary',
                  'date': '2026-05-20', 'timestamp': '2026-05-20T11:12:13+00:00', 'platform': 'A',
                  'orbit_pass': 'DESCENDING', 'relative_orbit': 42,
                  'source_polarizations': ['HH', 'HV']}

        def fixture_url(_, options):
            return MagicMock(url=lambda: options)

        def fetch(options, **kwargs):
            width, height = options['dimensions']
            col = round((options['crs_transform'][2] - grid.c) / grid.a)
            row = round((options['crs_transform'][5] - grid.f) / grid.e)
            values = np.full((height, width), -18.5 if col == 0 else -23.0, dtype='float32')
            if (col, row) == (0, 0):
                values[0, 0] = 0
            if col != 0 and row == 0:
                values[0, -1] = imagery._GEE_FLOAT_NODATA
            with MemoryFile() as tile:
                with tile.open(driver='GTiff', count=1, width=width, height=height, dtype='float32',
                               transform=Affine(*options['crs_transform']), crs=options['crs']) as raster:
                    raster.write(values, 1)
                return tile.read()

        def native_grid(asset, band):
            self.assertEqual((asset, band), ('COPERNICUS/S1_GRD/primary', 'HH'))
            return 770, 513, grid, CRS.from_epsg(32648)

        with tempfile.TemporaryDirectory() as root, patch.object(main, '_DOWNLOADS_DIR', Path(root)), \
             patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection') as roi, \
             patch.object(s1_mosaic, 'restore_s1_sources', return_value=SceneInfo(
                 's1gee', imagery.S1_COLLECTION, 'primary', '2026-05-20', -1, image,
                 scenes=({**source, 'footprint': footprint},))) as restore, \
             patch.object(full_scene_export, '_TILE_SIZE', 512), \
             patch.object(full_scene_export, '_native_grid', side_effect=native_grid), \
             patch.object(imagery, '_DownloadURL', side_effect=fixture_url), \
             patch.object(imagery, '_fetch_with_retry', side_effect=fetch) as fetch_tile, \
             patch.object(imagery.ee.data, 'exportImage', side_effect=AssertionError('No batch export expected')):
            client = TestClient(main.app)
            params = dict(extent='scene', start_date='2026-05-01', end_date='2026-05-31',
                          scene_ids='primary', polarization='hh', orbit_pass='DESCENDING', platform='A')
            response = client.get('/api/s1gee/download', params=params)
            self.assertEqual(response.status_code, 200, response.text[:100] if not response.is_success else '')
            self.assertCountEqual([call.args[0]['dimensions'] for call in fetch_tile.call_args_list],
                                  [[512, 512], [258, 512], [512, 1], [258, 1]])
            restore.assert_called_once_with(['primary'], None, '2026-05-01', '2026-06-01', 'hh', 'A', 'DESCENDING')
            roi.assert_not_called()
            self.assertIn('primary_hh_full_scene.tif', response.headers['content-disposition'])
            with MemoryFile(response.content) as memory, memory.open() as raster:
                self.assertEqual(raster.tags(ns='IMAGE_STRUCTURE').get('LAYOUT'), 'COG')
                self.assertTrue(raster.overviews(1))
                self.assertEqual(raster.shape, (513, 770))
                self.assertEqual(raster.crs.to_epsg(), 32648)
                self.assertEqual(raster.transform, grid)
                self.assertEqual(raster.descriptions, ('HH',))
                tags = raster.tags()
                self.assertEqual(tags['SOURCE_ASSET'], 'COPERNICUS/S1_GRD/primary')
                self.assertEqual(tags['SAR_POLARIZATION'], 'hh')
                self.assertEqual(tags['SAR_INSTRUMENT_MODE'], 'IW')
                self.assertEqual(tags['EXPORT_EXTENT'], 'full_scene')
                self.assertEqual(json.loads(tags['SAR_SOURCES']), [source])
                self.assertEqual(json.loads(tags['SAR_SOURCE_FOOTPRINTS']), {'primary': footprint})
                expected = np.full(raster.shape, -18.5, dtype='float32')
                expected[:, 512:] = -23
                expected[0, 0] = 0
                expected[0, -1] = np.nan
                values = raster.read(1, masked=True)
                np.testing.assert_array_equal(values.mask, np.isnan(expected))
                np.testing.assert_array_equal(values.filled(np.nan), expected)
            self.assertFalse(list(Path(root).iterdir()))
            self.assertEqual(client.get('/api/s1gee/download', params={**params,
                'scene_ids': ['primary', 'fill'], 'merge_scenes': 'true'}).status_code, 422)

    def test_option_metadata_and_download_link_all_modes(self):
        from dataclasses import replace
        result = replace(scene(), scene_id='s1gee_mosaic_2026-05-01_2026-06-01',
                         scene_count=2, coverage_pct=92,
                         scenes=({'scene_id': 'primary', 'date': '2026-05-20'},
                                 {'scene_id': 'fill', 'date': '2026-05-19'}))
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), None)), patch.object(main, 'best_s1_scene', return_value=scene()), patch.object(main, 'preview_rgb', return_value=None), patch.object(s1_mosaic, 'prepare_s1', return_value=result) as prepare:
            for mode in ('point', 'rectangle'):
                body = dict(start_date='2026-05-01', end_date='2026-05-31', merge_scenes=True, selection_mode=mode)
                body.update(dict(lon=107, lat=11) if mode == 'point' else dict(center_lon=107, center_lat=11, width_m=2000, height_m=1000))
                response = client.post('/api/s1gee/scene', json=body)
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertTrue(data['composite'])
                self.assertEqual(data['coverage_pct'], 92)
                self.assertIsNotNone(data['warning'])
                self.assertIn('merge_scenes=true', data['download_url'])
                self.assertEqual(len(data['scenes']), 2)
                self.assertTrue(prepare.call_args.args[-1])

    def test_administrative_modes_pass_merge_and_preserve_region_links(self):
        from dataclasses import replace
        result = replace(scene(), coverage_pct=88, scene_count=1)
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee'), patch.object(main, 'best_s1_scene_for_roi', return_value=scene()), patch.object(main, 'preview_rgb', return_value=None), patch.object(s1_mosaic, 'prepare_s1', return_value=result) as prepare:
            for mode in ('province', 'commune'):
                region = MagicMock(spec=main.Commune if mode == 'commune' else main.Province)
                region.id = '79'; region.name = 'Test'; region.province_name = 'Test'
                region.geometry = {'type': 'Polygon', 'coordinates': []}
                with patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), region)):
                    response = client.post('/api/s1gee/scene', json=dict(selection_mode=mode, **{mode + '_id': '79'}, start_date='2026-05-01', end_date='2026-05-31', merge_scenes=True))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('merge_scenes=true', response.json()['download_url'])
                self.assertIn(f'{mode}_id=79', response.json()['download_url'])
                self.assertFalse(response.json()['composite'])
                self.assertIsNotNone(response.json()['warning'])
                self.assertTrue(prepare.call_args.args[-1])

    def test_download_uses_mosaic_and_region_filename(self):
        from dataclasses import replace
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        sources = ({'scene_id': 'primary'}, {'scene_id': 'fill'})
        result = replace(scene(), scene_count=2, coverage_pct=92, scenes=sources)

        def make_raster(_urls, path, **_options):
            with rasterio.open(path, 'w', driver='GTiff', width=2, height=2, dtype='float32', count=1,
                               crs='EPSG:4326', transform=from_origin(106, 11, .1, .1)) as output:
                output.write(np.ones((2, 2), dtype='float32'), 1)

        with tempfile.TemporaryDirectory() as tmp, patch.object(main, '_DOWNLOADS_DIR', Path(tmp)), patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), None)), patch.object(main, 'best_s1_scene', return_value=scene()), patch.object(s1_mosaic, 'prepare_s1', return_value=result), patch.object(main, 'download_geotiff', return_value=[]) as download, patch.object(main, 'merge_band_tiffs', side_effect=make_raster):
            response = main.s1gee_download('2026-05-01', '2026-05-31', lon=107, lat=11, merge_scenes=True)
            self.assertIn('mosaic_2026-05-01_2026-05-31_n2_vvvh_pt', response.filename)
            self.assertIs(download.call_args.args[0], result.ee_object)
            with rasterio.open(response.path) as output:
                self.assertEqual(json.loads(output.tags()['SAR_SOURCES']), list(sources))
                self.assertEqual(output.tags()['SAR_MOSAIC_POLICY'], 'primary_first_fill_nodata')
                self.assertEqual(output.tags()['SAR_POLARIZATION'], 'vvvh')
                self.assertEqual(output.tags()['EXPORT_EXTENT'], 'roi')
                self.assertEqual(output.tags()['SAR_SELECTION_MODE'], 'point')

    def test_cdse_crop_mosaic_embeds_product_ids_and_safe_names(self):
        import rasterio
        from app.geo import RectangleROI

        def files(product_id, _credentials):
            return [{'key': f'eodata/{product_id}.SAFE/measurement/s1-grd-vv-data.tiff',
                     'rel': 'measurement/s1-grd-vv-data.tiff'}]

        def write_scene(_product_id, _roi, _bands, _files, _credentials,
                        _transform, _width, _height, output, _temporary, **_options):
            output.fill(1)

        roi = RectangleROI(center_lon=106, center_lat=11, width_m=1000, height_m=800)
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, '_DOWNLOADS_DIR', Path(tmp)), \
             patch.object(main, 'get_s3_credentials'), patch.object(main, 's3_list_product_files', side_effect=files), \
             patch.object(main, 'get_product_s3_path', return_value='/eodata/first.SAFE'), \
             patch.object(main, '_s1_grd_mosaic_product', side_effect=write_scene):
            path, _ = main._s1_crop_impl(['first', 'second'], roi, 100)
            with rasterio.open(path) as output:
                sources = json.loads(output.tags()['SAR_SOURCES'])
                self.assertEqual(sources, [{'product_id': 'first', 'product_name': 'first.SAFE'},
                                           {'product_id': 'second', 'product_name': 'second.SAFE'}])
                self.assertEqual(output.tags()['SAR_MOSAIC_POLICY'], 'last_valid_measurement_wins')
