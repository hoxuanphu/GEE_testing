"""Offline reference-header validation using synthetic GeoTIFFs."""
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from affine import Affine
import numpy as np
import rasterio

from app.reference_frame import read_reference_metadata, safe_tags
from app.frame_models import FrameError, FrameSearchRequest


class ReferenceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def raster(self, transform=Affine(3, 0, 398712, 0, -3, 2406162), crs='EPSG:32648'):
        path = self.root / 'reference.tif'
        with rasterio.open(path, 'w', driver='GTiff', width=15, height=12, count=1,
                           dtype='uint8', crs=crs, transform=transform) as dst:
            dst.write(np.zeros((1, 12, 15), dtype='uint8'))
            dst.update_tags(ACQUISITION_DATE='2026-03-28', password='do not expose')
        return path

    def test_header_and_tag_allowlist(self):
        info = read_reference_metadata(self.raster(), '..\\private\\ảnh.tif')
        self.assertEqual(info['display_name'], 'ảnh.tif')
        self.assertEqual(info['width'], 15)
        self.assertEqual(info['pixel_size'], [3, 3])
        self.assertEqual(info['tags'], {'ACQUISITION_DATE': '2026-03-28'})
        self.assertEqual(len(info['footprint_geojson']['coordinates'][0]), 129)
        self.assertEqual(info['corners'][0], [398712, 2406162])
        self.assertEqual(info['compatible_grid_modes'], ['match_extent', 'match_grid'])

    def test_rotated_footprint_and_geographic_mode(self):
        t = Affine.translation(104, 22) * Affine.rotation(20) * Affine.scale(.0001, -.0001)
        info = read_reference_metadata(self.raster(t, 'EPSG:4326'), 'sample.tif')
        self.assertEqual(info['compatible_grid_modes'], ['match_grid'])
        self.assertAlmostEqual(info['corners'][1][0], (t * (15, 0))[0])
        self.assertNotEqual(info['corners'][0][1], info['bounds'][3])

    def test_invalid_headers_and_non_tiff(self):
        with self.assertRaises(FrameError):
            read_reference_metadata(self.raster(crs=None), 'bad.tif')
        path = self.root / 'fake.tif'
        path.write_text('<VRTDataset/>')
        with self.assertRaises(FrameError) as ctx:
            read_reference_metadata(path, 'fake.tif')
        self.assertEqual(ctx.exception.status, 415)

    def test_sanitize_orbit_tags(self):
        tags = safe_tags({'RELATIVE_ORBIT': '176', 'ORBIT_PASS': 'invalid',
                          'SOURCE_SCENE': 'https://private', 'PLATFORM': 'A'})
        self.assertEqual(tags, {'PLATFORM': 'A'})

    def test_roi_identity_recomputes_reference_and_rejects_mutations(self):
        import copy
        import hashlib
        import json
        from app import reference_frame
        self.assertTrue(hasattr(reference_frame, 'reference_roi_identity'))
        for frame in (
            dict(crs='EPSG:32648', transform=[10, 2, 398000, 1, -10, 2400000], width=8, height=5),
            dict(crs='EPSG:4326', transform=[.001, .0001, 104, .0002, -.001, 22], width=8, height=5),
            dict(crs='+proj=eqc +lat_ts=21.7423783969593 +lat_0=21.7423783969593 '
                     '+lon_0=104.035720835673 +R=6378166.17539632 +units=m',
                 transform=[9.993915937710756, 0, -8065.090161732581, 0, -9.99956743608598, 8094.649839511601],
                 width=1614, height=1619)):
            identity = reference_frame.reference_roi_identity(frame)
            params = dict(reference=frame, **identity)
            reference_frame.validate_roi_identity(params)
            self.assertEqual(identity['roi_version'], 2)
            canonical = json.dumps({'version': 2, 'geometry': identity['roi_geojson']},
                                   sort_keys=True, separators=(',', ':'), allow_nan=False)
            self.assertEqual(identity['roi_fingerprint'], hashlib.sha256(canonical.encode()).hexdigest())
            for mutation in ('missing', 'version', 'geometry', 'fingerprint', 'affine', 'width', 'crs'):
                changed = copy.deepcopy(params)
                if mutation == 'missing':
                    changed.pop('roi_version')
                elif mutation == 'version':
                    changed['roi_version'] = 1
                elif mutation == 'geometry':
                    changed['roi_geojson']['coordinates'][0][0][1] += 1
                elif mutation == 'fingerprint':
                    changed['roi_fingerprint'] = '0' * 64
                elif mutation == 'affine':
                    changed['reference']['transform'][2] += 1
                elif mutation == 'width':
                    changed['reference']['width'] += 1
                else:
                    changed['reference']['crs'] = 'invalid-crs'
                with self.subTest(mutation=mutation), self.assertRaises(FrameError) as caught:
                    reference_frame.validate_roi_identity(changed)
                self.assertEqual((caught.exception.code, caught.exception.status), ('stale_search', 409))
            latitudes = [point[1] for point in identity['roi_geojson']['coordinates'][0]]
            self.assertGreater(min(latitudes), 21.6)
            self.assertLess(max(latitudes), 22.1)

    def test_request_contract(self):
        import uuid
        body = dict(reference_id=str(uuid.uuid4()), start_date='2026-03-01', end_date='2026-05-31')
        self.assertEqual(FrameSearchRequest(**body).band_preset, 'full')
        for extra in ({'sort': 'nearest_date'}, {'prefer_reference_orbit': True},
                      {'end_date': '2026-02-01'}, {'arbitrary_path': 'secret'}):
            with self.assertRaises(ValueError):
                FrameSearchRequest(**{**body, **extra})

    def test_roi_identity_rejects_boolean_coordinates_equal_to_numbers(self):
        from app.reference_frame import reference_roi_identity, validate_roi_identity
        frame = dict(crs='EPSG:4326', transform=[.001, 0, 0, 0, -.001, 1], width=2, height=2)
        params = dict(reference=frame, **reference_roi_identity(frame))
        params['roi_geojson']['coordinates'][0][0][0] = False
        with self.assertRaises(FrameError) as caught:
            validate_roi_identity(params)
        self.assertEqual(caught.exception.code, 'stale_search')
