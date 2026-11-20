"""Offline rectangle contracts, local raster exports and persisted Esri jobs."""
import json
import math
import shutil
import threading
import unittest
import uuid
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import rasterio
from affine import Affine
from PIL import Image
from rasterio.warp import transform

from app import esri, raster_grid
from app.geo import RectangleROI


def rectangle(**overrides):
    return esri.EsriRequest(**{
        'selection_mode': 'rectangle', 'center_lon': 105, 'center_lat': 60,
        'width_m': 180, 'height_m': 70, 'rotation_deg': 30, 'zoom': 14,
        **overrides,
    })


class RectangleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ('esri-rectangle-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.services = []
        self.calls = []
        self.color = (48, 96, 144)
        self.missing = lambda x, y: False
        owner = self

        class Response:
            status_code = 200

            def __init__(self, x, y):
                self.headers = {'blank-tile': 'true'} if owner.missing(x, y) else {}
                buffer = BytesIO()
                Image.new('RGB', (256, 256), owner.color).save(buffer, format='JPEG')
                self.data = buffer.getvalue()

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield self.data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class Session:
            def __init__(self):
                self.headers = {}

            def get(self, url, **kwargs):
                owner.calls.append(url)
                _, y, x = url.rsplit('/', 2)
                return Response(int(x), int(y))

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(self.shutdown)
        self.session_patch = patch.object(esri.requests, 'Session', Session)
        self.ca_patch = patch.object(esri, '_load_cas')
        self.session_patch.start()
        self.ca_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.addCleanup(self.ca_patch.stop)

    def shutdown(self):
        for service in self.services:
            service.shutdown()
            if service._thread:
                service._thread.join(5)

    def service(self):
        service = esri.EsriService(self.root / 'jobs')
        self.services.append(service)
        return service

    def finish(self, service, job_id):
        service._thread.join(10)
        self.assertFalse(service._thread.is_alive())
        return service.get(job_id)

    def test_model_roundtrip_and_rotation_normalization(self):
        for angle in (0, 1, 30, 90, 180, 270, 359, 360):
            with self.subTest(angle=angle):
                body = rectangle(rotation_deg=angle)
                self.assertEqual(body.rotation_deg, angle % 360)
                restored = esri.EsriRequest.model_validate_json(body.model_dump_json())
                self.assertEqual(restored, body)
                plan = esri.request_plan(restored)
                self.assertIsNotNone(plan.output_grid)
                self.assertTrue(plan.source_halo)
                self.assertEqual(plan.rotation_deg, angle % 360)

    def test_incomplete_and_mixed_selections_are_rejected(self):
        base = rectangle().model_dump()
        for field in ('center_lon', 'center_lat', 'width_m', 'height_m'):
            for value in ('omitted', None):
                data = dict(base)
                if value == 'omitted':
                    data.pop(field)
                else:
                    data[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    esri.EsriRequest(**data)
        invalid = [
            {**base, 'lon': 105}, {**base, 'lat': 21}, {**base, 'radius_m': 50},
            {**base, 'bbox': [104, 20, 106, 22]}, {**base, 'commune_id': '001'},
            {**base, 'selection_mode': 'point'}, {**base, 'selection_mode': 'bbox'},
            {**base, 'selection_mode': None}, {**base, 'selection_mode': 'unsupported'},
            {'selection_mode': 'rectangle', 'bbox': [104, 20, 106, 22]},
            {'bbox': [104, 20, 106, 22], 'width_m': 50},
            {'lon': 105, 'lat': 21, 'radius_m': 50, 'center_lon': 105},
            {'commune_id': '001', 'height_m': 50},
            {'bbox': [104, 20, 106, 22], 'selection_mode': 'point'},
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                esri.EsriRequest(**data)

    def test_finite_values_dimensions_domain_and_existing_limits(self):
        for field in ('center_lon', 'center_lat', 'width_m', 'height_m', 'rotation_deg'):
            for value in (float('nan'), float('inf'), -float('inf')):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    rectangle(**{field: value})
        invalid = [
            {'width_m': 0}, {'height_m': -1}, {'width_m': 90001}, {'height_m': 90001},
            {'center_lon': 181}, {'center_lat': 86}, {'center_lon': 180},
            {'center_lon': -180}, {'center_lat': esri.MAX_LAT},
            {'rotation_deg': -1}, {'rotation_deg': 361}, {'zoom': 20},
            {'workers': 0}, {'workers': 9}, {'max_tiles': 0}, {'max_tiles': 20001},
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                rectangle(**overrides)
        self.assertFalse(self.calls)

    def test_polygon_grid_scale_and_full_interpolation_halo(self):
        for angle in (0, 30, 90, 360):
            with self.subTest(angle=angle):
                body = rectangle(rotation_deg=angle)
                roi = RectangleROI(**{key: getattr(body, key) for key in (
                    'center_lon', 'center_lat', 'width_m', 'height_m', 'rotation_deg')})
                geometry, bbox = roi.model_geometry()
                plan = esri.request_plan(body)
                self.assertEqual(plan.roi_geometry, geometry)
                self.assertEqual(plan.bbox, [bbox.west, bbox.south, bbox.east, bbox.north])
                mercator_pixel = 2 * esri.ORIGIN_SHIFT / (2 ** body.zoom * esri.TILE_SIZE)
                scale = mercator_pixel * (111320 / (esri.ORIGIN_SHIFT / 180)) * .5
                self.assertEqual(plan.output_grid['width'], math.ceil(body.width_m / scale))
                self.assertEqual(plan.output_grid['height'], math.ceil(body.height_m / scale))
                unpadded = esri.make_plan(plan.bbox, body.zoom, geometry=geometry, source_halo=True)
                self.assertEqual(plan.x0, unpadded.x0 - 1)
                self.assertEqual(plan.y0, unpadded.y0 - 1)
                self.assertEqual(plan.width, unpadded.width + 512)
                self.assertEqual(plan.height, unpadded.height + 512)
                self.assertEqual(len(plan.tiles), plan.width * plan.height // 256**2)
                self.assertTrue(all(np.all(plan.mask(*tile) == 255) for tile in plan.tiles))
                if angle == 30:
                    ring = geometry['coordinates'][0][:-1]
                    self.assertGreater(len({p[0] for p in ring}), 2)
                    self.assertGreater(len({p[1] for p in ring}), 2)

    def test_actual_tiny_raster_export_footprint_and_valid_edges(self):
        for angle in (0, 30, 90, 360):
            with self.subTest(angle=angle):
                plan = esri.request_plan(rectangle(rotation_deg=angle))
                output = self.root / f'rectangle-{angle}.tif'
                stats = esri.download(plan, output, self.root / 'cache')
                self.assertEqual(stats['failed'], 0)
                with rasterio.open(output) as src:
                    self.assertEqual(src.count, 3)
                    self.assertEqual(src.shape, (plan.output_grid['height'], plan.output_grid['width']))
                    self.assertEqual(src.transform, Affine(*plan.output_grid['transform']))
                    self.assertTrue(math.isnan(src.nodata))
                    self.assertTrue(np.all(src.dataset_mask() == 255))
                    np.testing.assert_allclose(src.read(), np.broadcast_to(
                        np.array(self.color)[:, None, None], (3, src.height, src.width)), atol=2)
                    tags = src.tags()
                    self.assertEqual(json.loads(tags['roi_geometry']), plan.roi_geometry)
                    self.assertEqual(json.loads(tags['bbox']), plan.bbox)
                    self.assertEqual(float(tags['rotation_deg']), angle % 360)
                    self.assertEqual(json.loads(tags['output_grid']),
                                     json.loads(json.dumps(plan.output_grid)))
                    corners = [src.transform * p for p in (
                        (0, 0), (src.width, 0), (src.width, src.height), (0, src.height))]
                    xs, ys = transform(src.crs, 'EPSG:4326', *zip(*corners))
                    vertices = plan.roi_geometry['coordinates'][0][:-1]
                    for x, y in zip(xs, ys):
                        self.assertLess(min(math.hypot(x-v[0], y-v[1]) for v in vertices), 1e-7)
        self.assertFalse(list(self.root.rglob('*.part')))
        self.assertFalse(list(self.root.rglob('*.msk')))

    def test_subpixel_rectangle_still_exports_one_valid_pixel(self):
        plan = esri.request_plan(rectangle(width_m=.01, height_m=.02, rotation_deg=0))
        self.assertEqual((plan.output_grid['width'], plan.output_grid['height']), (1, 1))
        output = self.root / 'subpixel.tif'
        esri.download(plan, output, self.root / 'cache')
        with rasterio.open(output) as src:
            self.assertEqual(src.shape, (1, 1))
            self.assertEqual(src.dataset_mask()[0, 0], 255)
            self.assertAlmostEqual(src.transform.a, .01)
            self.assertAlmostEqual(abs(src.transform.e), .02)

    def test_cancel_before_publish_preserves_previous_output(self):
        plan = esri.request_plan(rectangle(rotation_deg=0))
        output = self.root / 'cancelled.tif'
        output.write_bytes(b'previous complete output')
        cancel = threading.Event()
        original = raster_grid.warp_to_grid

        def cancelled_warp(*args, **kwargs):
            original(*args, **kwargs)
            cancel.set()

        with patch.object(raster_grid, 'warp_to_grid', side_effect=cancelled_warp):
            with self.assertRaises(esri.CancelledError):
                esri.download(plan, output, self.root / 'cache', cancel=cancel, overwrite=True)
        self.assertEqual(output.read_bytes(), b'previous complete output')
        self.assertFalse(list(self.root.rglob('*.part')))
        self.assertFalse(list(self.root.rglob('*.msk')))

    def test_valid_black_pixels_and_missing_tile_nodata(self):
        plan = esri.request_plan(rectangle(center_lon=0, center_lat=1,
                                           width_m=2400, height_m=900, zoom=14))
        self.color = (0, 0, 0)
        output = self.root / 'black.tif'
        esri.download(plan, output, self.root / 'black-cache')
        with rasterio.open(output) as src:
            self.assertTrue(np.all(src.dataset_mask() == 255))
            self.assertTrue(np.all(src.read() == 0))
        self.missing = lambda x, y: x >= 8192
        output = self.root / 'partial.tif'
        stats = esri.download(plan, output, self.root / 'partial-cache')
        self.assertGreater(stats['failed'], 0)
        with rasterio.open(output) as src:
            valid = src.dataset_mask() > 0
            self.assertTrue(valid.any())
            self.assertTrue((~valid).any())
            self.assertTrue(np.all(src.read()[:, valid] == 0))
            self.assertTrue(np.all(np.isnan(src.read()[:, ~valid])))

    def test_resource_limits_before_job_creation(self):
        body = rectangle()
        plan = esri.request_plan(body)
        self.assertEqual(esri._raster_bytes(plan), plan.width * plan.height * 5 +
                         plan.output_grid['width'] * plan.output_grid['height'] * 17)
        service = self.service()
        with self.assertRaisesRegex(ValueError, 'tiles'):
            service.start(rectangle(max_tiles=1))
        for limit, message in (('MAX_PIXELS', 'pixels'), ('MAX_JOB_DISK', 'budget'),
                               ('MAX_SERVICE_DISK', 'budget')):
            with patch.object(esri, limit, 1), self.assertRaisesRegex(ValueError, message):
                service.start(body)
        with patch.object(esri.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError, 'free disk'):
                service.start(body)
        with patch.object(RectangleROI, 'output_grid', return_value={
                **plan.output_grid, 'width': esri.MAX_PIXELS, 'height': 2}):
            with self.assertRaisesRegex(ValueError, 'pixels'):
                service.start(body)
        self.assertEqual(list(service.root.iterdir()), [])
        self.assertFalse(self.calls)

    def test_snapshot_restart_resume_reuses_grid_and_verified_cache(self):
        for angle in (0, 30):
            with self.subTest(angle=angle):
                body = rectangle(rotation_deg=angle)
                service = self.service()
                with patch.object(raster_grid, 'warp_to_grid', side_effect=RuntimeError('warp failed')):
                    job_id = service.start(body)['job_id']
                    failed = self.finish(service, job_id)
                self.assertEqual(failed['status'], 'failed')
                self.assertIsNone(service.download_path(job_id))
                original = json.loads((service.root / job_id / 'job.json').read_text())
                self.assertEqual(original['request']['selection_mode'], 'rectangle')
                self.assertEqual(original['rotation_deg'], angle)
                self.assertIsNotNone(original['output_grid'])
                body.width_m = 900
                service.shutdown()
                recovered = self.service()
                calls = len(self.calls)
                with patch.object(RectangleROI, 'output_grid', side_effect=AssertionError('use snapshot')):
                    recovered.resume(job_id)
                    done = self.finish(recovered, job_id)
                self.assertEqual(done['status'], 'completed')
                self.assertEqual(len(self.calls), calls)
                self.assertEqual(done['progress']['cached'], done['progress']['total'])
                for key in ('request', 'bbox', 'roi_geometry', 'rotation_deg', 'output_grid'):
                    self.assertEqual(done[key], original[key])
                with rasterio.open(recovered.download_path(job_id)) as src:
                    self.assertEqual(json.loads(src.tags()['output_grid']), original['output_grid'])
                    self.assertTrue(np.all(src.dataset_mask() == 255))
        self.assertFalse(list(self.root.rglob('*.part')))

    def test_snapshot_preserves_zero_angle_halo_and_checks_output_limit(self):
        body = rectangle(rotation_deg=0)
        plan = esri.request_plan(body)
        snapshot = json.loads(json.dumps({
            'bbox': plan.bbox, 'roi_geometry': plan.roi_geometry, 'output_grid': plan.output_grid}))
        resumed = esri.request_plan(body, snapshot)
        self.assertEqual(resumed.tiles, plan.tiles)
        self.assertTrue(resumed.source_halo)
        self.assertEqual(resumed.transform, plan.transform)
        snapshot['output_grid']['width'] = esri.MAX_PIXELS
        with self.assertRaisesRegex(ValueError, 'pixels'):
            esri.request_plan(body, snapshot)

    def test_corrupt_cached_tile_is_refetched_on_resume(self):
        service = self.service()
        with patch.object(raster_grid, 'warp_to_grid', side_effect=RuntimeError('warp failed')):
            job_id = service.start(rectangle())['job_id']
            self.finish(service, job_id)
        cached = next((service.root / job_id / 'cache').glob('*.jpg'))
        cached.write_bytes(b'not a JPEG')
        before = len(self.calls)
        service.resume(job_id)
        done = self.finish(service, job_id)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(len(self.calls) - before, 1)
        self.assertEqual(done['progress']['cached'], done['progress']['total'] - 1)

    def test_partial_job_resume_fetches_missing_tiles(self):
        service = self.service()
        self.missing = lambda x, y: x >= 8192
        job_id = service.start(rectangle(center_lon=0, center_lat=1,
                                          width_m=2400, height_m=900))['job_id']
        partial = self.finish(service, job_id)
        self.assertEqual(partial['status'], 'partial')
        self.assertGreater(partial['progress']['failed'], 0)
        self.missing = lambda x, y: False
        before = len(self.calls)
        service.resume(job_id)
        done = self.finish(service, job_id)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(len(self.calls) - before, partial['progress']['failed'])
        self.assertEqual(done['progress']['cached'], partial['progress']['succeeded'])
        with rasterio.open(service.download_path(job_id)) as src:
            self.assertTrue(np.all(src.dataset_mask() == 255))


if __name__ == '__main__':
    unittest.main()
