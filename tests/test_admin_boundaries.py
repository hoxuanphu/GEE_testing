"""Administrative geometry is consistent with the packaged source."""
import unittest

from shapely.geometry import shape

from app.admin_boundaries import get_province, province_feature_collection


class ProvinceGeometryTests(unittest.TestCase):
    def test_quang_ninh_keeps_the_mainland_and_islands_as_exteriors(self):
        province = get_province('VN22')
        geometry = shape(province.geometry)
        self.assertTrue(geometry.is_valid)
        self.assertGreater(geometry.area, 0.5)
        self.assertGreater(len(geometry.geoms), 1000)

    def test_map_serves_valid_geometry_for_every_province(self):
        features = province_feature_collection()['features']
        self.assertEqual(len(features), 34)
        self.assertTrue(all(shape(feature['geometry']).is_valid for feature in features))
