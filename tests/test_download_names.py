"""Full product IDs must survive GEE download filenames."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import main
from app.geo import Bbox


class DownloadNameTests(unittest.TestCase):
    def test_sar_preserves_full_id_in_point_and_administrative_names(self):
        product = 'S1A_IW_GRDH_1SDV_20260520T110616_20260520T110641_064123_080ABC_1234'
        scene = SimpleNamespace(scene_id=product, date='2026-05-20', ee_object=object())
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, '_DOWNLOADS_DIR', Path(tmp)), patch.object(main, '_ensure_ee'), patch.object(main, 'best_s1_scene', return_value=scene), patch.object(main, 'best_s1_scene_for_roi', return_value=scene), patch.object(main, 'download_geotiff', return_value=[]), patch.object(main, 'merge_band_tiffs'):
            for mode in ('point', 'province', 'commune'):
                region = None if mode == 'point' else SimpleNamespace(id='79')
                with self.subTest(mode=mode), patch.object(main, '_resolve_selection', return_value=(object(), Bbox(106, 10, 107, 11), region)):
                    response = main.s1gee_download('2026-05-01', '2026-05-31', selection_mode=mode, lon=107, lat=11)
                    self.assertIn(product, response.filename)
                    self.assertIn('_vvvh_', response.filename)
                    self.assertIn('_pt107.00000_11.00000_r2000', response.filename) if mode == 'point' else self.assertIn(f'_{mode}_79', response.filename)
                    self.assertLess(len(Path(response.path).name), 40)

    def test_optical_preserves_full_scene_id(self):
        product = '20260520T031541_20260520T032219_T48PXS_EXTRA_PRODUCT_ID'
        scene = SimpleNamespace(scene_id=product, date='2026-05-20', ee_object=object())
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, '_DOWNLOADS_DIR', Path(tmp)), patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection', return_value=(object(), Bbox(106, 10, 107, 11), None)), patch.object(main, 'best_scene', return_value=scene), patch.object(main, 'download_geotiff', return_value=[]), patch.object(main, 'merge_band_tiffs'):
            response = main.download('sentinel2', '2026-05-01', '2026-05-31', lon=107, lat=11)
            self.assertIn(product, response.filename)
            self.assertLess(len(Path(response.path).name), 40)
