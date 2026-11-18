"""Projection fallback regression tests with bounded local fixtures."""
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest import mock
from uuid import uuid4

import numpy as np
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from rasterio.warp import reproject
from rasterio.windows import Window

from app import frame_export as export, frame_projection as projection, frame_search as search

SPHERE = '+proj=eqc +lat_ts=21.7423783969593 +lat_0=21.7423783969593 +lon_0=104.035720835673 +R=6378166.17539632 +units=m +no_defs'
GRID = dict(crs=SPHERE, transform=[9.993915937710756, 0, -8065.090161732581, 0, -9.99956743608598, 8094.649839511601], width=1614, height=1619)


class HTTPTests(unittest.TestCase):
    def rpc(self, status, body):
        rpc = object.__new__(search._RPC)
        rpc.base, rpc.timeout, rpc.deadline = 'https://example.invalid', 10, None
        rpc.session = mock.MagicMock()
        response = rpc.session.request.return_value.__enter__.return_value
        response.status_code = status
        response.iter_content.return_value = iter([body])
        return rpc

    def test_status_classification_safe_and_nonretryable(self):
        for status, body, code in (
            (400, {'error': {'status': 'INVALID_ARGUMENT', 'message': 'Unable to write GeoTIFFs in projection secret'}}, 'unsupported_export_projection'),
            (400, {'error': {'message': 'secret'}}, 'invalid_upstream_request'),
            (401, {}, 'upstream_unauthenticated'), (403, {}, 'upstream_forbidden'),
            (400, None, 'invalid_upstream_request')):
            rpc = self.rpc(status, json.dumps(body).encode() if body else b'not JSON secret')
            with self.subTest(status=status, code=code), self.assertRaises(search.FrameUpstreamError) as caught:
                search._retry(lambda: rpc.request_once('GET', 'test'), time.monotonic()+30)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.http_status, status)
            self.assertNotIn('secret', str(caught.exception))
            self.assertEqual(rpc.session.request.call_count, 1)

    def test_transient_retries_bounded(self):
        for status in (408, 429, 500):
            rpc = self.rpc(status, b'')
            with mock.patch.object(search.time, 'sleep'), self.assertRaises(search.FrameUpstreamError) as caught:
                search._retry(lambda: rpc.request_once('GET', 'test'), time.monotonic()+30)
            self.assertEqual(caught.exception.code, {408:'timeout',429:'upstream_quota',500:'upstream_unavailable'}[status])
            self.assertEqual(rpc.session.request.call_count, 3)

    def test_oversized_body_stops_reading(self):
        rpc = self.rpc(400, b'')
        chunks = mock.Mock(side_effect=AssertionError('unbounded read'))
        def body():
            yield b'x'*4097
            chunks()
        rpc.session.request.return_value.__enter__.return_value.iter_content.return_value = body()
        with self.assertRaises(search.FrameUpstreamError) as caught:
            rpc.request_once('GET', 'test')
        self.assertEqual(caught.exception.code, 'invalid_upstream_request')
        chunks.assert_not_called()


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path('.projection-test-'+uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def test_original_grid_and_resource_plan(self):
        plan = projection.plan_transport_grid(GRID, 's1gee', ['VV','VH'])
        self.assertEqual(plan['grid']['crs'], 'EPSG:32648')
        self.assertLess(plan['grid']['width'], 1700)
        self.assertGreater(plan['disk_reserve_bytes'], 300_000_000)
        self.assertEqual(GRID['width'], 1614)

    def test_geographic_cross_zone_pole_and_dateline(self):
        for lon, lat in ((104, 20), (5.9, 20), (10, 85)):
            grid = dict(crs='EPSG:4326', transform=[.01,0,lon,0,-.01,lat], width=30,height=30)
            plan = projection.plan_transport_grid(grid, 's1gee', ['VV'])
            self.assertLess(plan['grid']['width'], 100)
        for grid, code in ((dict(crs='EPSG:4326', transform=[.01,0,179.9,0,-.01,20], width=30,height=30), 'unsupported_transport_extent'),
                           ({**GRID,'crs':'+proj=tmerc +lon_0=104 +R=6378166 +units=m'}, 'unsupported_reference_crs')):
            with self.assertRaises(export.FrameExportError) as caught:
                projection.plan_transport_grid(grid,'s1gee',['VV'])
            self.assertEqual(caught.exception.code, code)

    def test_windowed_warp_equals_mosaic_oracle_masks_values_and_cleanup(self):
        for dataset in ('s1gee','sentinel2'):
            grid = dict(crs=SPHERE, transform=[10,2,-100,1,-10,100], width=35,height=33)
            transport = projection.plan_transport_grid(grid, dataset, ['VV','VH'])['grid']
            h,w = transport['height'],transport['width']
            data = np.resize(np.array([0,-45,17,-10],np.float32),(2,h,w))
            invalid = np.zeros(data.shape, bool)
            invalid[0,8:15,8:15] = True
            invalid[1,12:20,12:20] = True
            if dataset == 'sentinel2':
                data *= .0001  # already scaled exactly once by load_frame_image
            def fetch(image, nearest_dataset, bands, source, window, **kwargs):
                self.assertEqual(nearest_dataset,'s1gee')
                i = ['VV','VH'].index(bands[0]); y,x = int(window.row_off),int(window.col_off)
                return np.ma.array(data[i:i+1,y:y+int(window.height),x:x+int(window.width)],
                    mask=invalid[i:i+1,y:y+int(window.height),x:x+int(window.width)])
            output_path = self.root / (dataset+'.tif')
            original_tiles = export.iter_grid_tiles
            def small_tiles(source, count):
                if source == grid:
                    for y in range(0,grid['height'],7):
                        for x in range(0,grid['width'],9):
                            yield Window(x,y,min(9,grid['width']-x),min(7,grid['height']-y))
                else:
                    yield from original_tiles(source,count)
            with rasterio.open(output_path,'w',driver='GTiff',count=2,dtype='float32',nodata=np.nan,
                    crs=grid['crs'], transform=Affine(*grid['transform']),width=grid['width'],height=grid['height']) as output:
                with mock.patch.object(export,'fetch_grid_tile',side_effect=fetch), mock.patch.object(export,'iter_grid_tiles',side_effect=small_tiles):
                    provenance = projection.export_via_transport_grid('image',dataset,['VV','VH'],grid,output,rpc='rpc')
            expected = np.full((2,grid['height'],grid['width']),np.nan,np.float32)
            masks = np.zeros(expected.shape,np.uint8)
            options = dict(src_transform=Affine(*transport['transform']),src_crs=transport['crs'],dst_transform=Affine(*grid['transform']),dst_crs=grid['crs'])
            reproject(np.where(invalid,np.nan,data),expected,src_nodata=np.nan,dst_nodata=np.nan,
                resampling=Resampling.bilinear if dataset=='sentinel2' else Resampling.nearest,**options)
            reproject((~invalid).astype('uint8'),masks,resampling=Resampling.nearest,**options)
            expected[:,~masks.all(axis=0)] = np.nan
            with rasterio.open(output_path) as result:
                np.testing.assert_allclose(result.read(),expected,rtol=1e-5,atol=1e-6,equal_nan=True)
            self.assertEqual(provenance['value_resampling_steps'][0],'nearest')
            self.assertEqual(list(self.root.glob('*.values.tif')),[])
            self.assertEqual(list(self.root.glob('*.masks.tif')),[])

    def test_resource_limits_fail_before_download(self):
        grid = {**GRID, 'width':30000,'height':30000}
        with self.assertRaises(export.FrameExportError), mock.patch.object(export,'fetch_grid_tile') as fetch:
            projection.plan_transport_grid(grid,'s1gee',['VV'])
        fetch.assert_not_called()

    def test_transport_failure_cleans_intermediates(self):
        grid = dict(crs=SPHERE, transform=[10,0,-100,0,-10,100],width=10,height=10)
        for failure in (OSError(28,'disk full'), search.FrameUpstreamError('upstream_failed','safe')):
            path = self.root / 'failure.tif'
            with rasterio.open(path,'w',driver='GTiff',count=1,dtype='float32',nodata=np.nan,
                    crs=grid['crs'],transform=Affine(*grid['transform']),width=10,height=10) as output:
                with mock.patch.object(export,'fetch_grid_tile',side_effect=failure):
                    with self.assertRaises(type(failure)):
                        projection.export_via_transport_grid('image','s1gee',['VV'],grid,output,rpc='rpc')
            self.assertEqual(list(self.root.glob('*.values.tif')),[])
            self.assertEqual(list(self.root.glob('*.masks.tif')),[])

    def test_custom_crs_never_passed_to_ee_even_if_writer_would_accept_it(self):
        grid = dict(crs=SPHERE, transform=[10, 0, -100, 0, -10, 100], width=10, height=10)
        params = dict(dataset='s1gee', bands=['VV'], output_grid=grid, reference=grid, require_full_coverage=True)
        scene = dict(asset_id='COPERNICUS/S1_GRD/scene', date='2026-01-01')
        def tile(image, dataset, bands, target, window, **kwargs):
            self.assertTrue(target['crs'].startswith('EPSG:'))
            return np.ma.array(np.full((1, int(window.height), int(window.width)), -15, np.float32))
        path = self.root / 'custom.tif'
        with mock.patch.object(export, '_RPC'), mock.patch.object(export, 'load_frame_image'), \
                mock.patch.object(export, 'fetch_grid_tile', side_effect=tile):
            report = export.export_frame_scene(scene, params, path)
        self.assertTrue(report['verified'])
        self.assertEqual(report['export_provenance']['projection_fallback_reason'], 'custom_reference_projection')
        self.assertEqual(report['actual_coverage_pct'], {'VV': 100})
        with rasterio.open(path) as result:
            self.assertEqual(list(result.transform)[:6], grid['transform'])
            self.assertEqual(result.width, 10)

    def test_only_projection_error_falls_back_and_verifies(self):
        grid = dict(crs='EPSG:32648',transform=[10,0,400000,0,-10,2400000],width=8,height=8)
        params = dict(dataset='s1gee',bands=['VV'],output_grid=grid,reference=grid,require_full_coverage=True)
        scene = dict(asset_id='COPERNICUS/S1_GRD/scene',date='2026-01-01')
        def fallback(image,dataset,bands,grid,output,**kwargs):
            output.write(np.zeros((1,8,8),np.float32))
            transport = projection.plan_transport_grid(grid,dataset,bands)['grid']
            return dict(version=1,export_strategy='standard_grid_local_warp',transport_crs=transport['crs'],transport_resolution=transport['transform'][0],
                        value_resampling_steps=['nearest','nearest'],projection_fallback_reason='unsupported_export_projection')
        for code in ('unsupported_export_projection','upstream_forbidden','upstream_failed','invalid_upstream_request'):
            path = self.root / (code+'.tif')
            with mock.patch.object(export,'_RPC'), mock.patch.object(export,'load_frame_image'), mock.patch.object(export,'fetch_grid_tile',side_effect=search.FrameUpstreamError(code,'safe')), mock.patch.object(projection,'export_via_transport_grid',side_effect=fallback) as warp:
                if code == 'unsupported_export_projection':
                    report = export.export_frame_scene(scene,params,path)
                    self.assertTrue(report['verified'])
                    self.assertEqual(report['actual_coverage_pct'],{'VV':100})
                    self.assertEqual(export.build_frame_manifest(params,[report])['reports'][0]['export_provenance']['export_strategy'],'standard_grid_local_warp')
                else:
                    with self.assertRaises(search.FrameUpstreamError):
                        export.export_frame_scene(scene,params,path)
                    warp.assert_not_called()
                    self.assertFalse(path.exists())
            self.assertEqual(list(self.root.glob('*.part.tif')),[])
