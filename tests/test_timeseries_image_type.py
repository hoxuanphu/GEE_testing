"""Regression: Earth Engine copyProperties may return Element, not Image."""
import unittest
from unittest import mock

from app import imagery


class TestTimeSeriesImageType(unittest.TestCase):
    def test_scaled_element_is_cast_before_select_and_return(self):
        source = mock.Mock()
        element = object()
        for dataset, processor in [('sentinel2', '_scale_s2'), ('landsat', '_landsat_scale')]:
            with self.subTest(dataset=dataset), \
                    mock.patch.object(imagery, processor, return_value=element), \
                    mock.patch.object(imagery.ee, 'Image') as image:
                selected = image.return_value.select.return_value
                copied = selected.copyProperties.return_value
                result = imagery._process_timeseries_image(source, dataset, 'vvvh')
                self.assertEqual(image.call_args_list, [mock.call(element), mock.call(copied)])
                image.return_value.select.assert_called_once_with(
                    imagery.timeseries_source_metadata(dataset)['bands'])
                selected.copyProperties.assert_called_once_with(source, source.propertyNames.return_value)
                self.assertIs(result, image.return_value)

    def test_other_datasets_keep_bands_and_metadata(self):
        for dataset in ('nicfi', 'dynamic_world', 's1gee'):
            with self.subTest(dataset=dataset), mock.patch.object(imagery.ee, 'Image') as image:
                source = mock.Mock()
                result = imagery._process_timeseries_image(source, dataset, 'vvvh')
                image.return_value.select.assert_called_once_with(
                    imagery.timeseries_source_metadata(dataset)['bands'])
                self.assertIs(result, image.return_value)
