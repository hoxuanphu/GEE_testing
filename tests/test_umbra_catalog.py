import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from shapely.geometry import box, mapping

from app.umbra_catalog import Catalog, Object, PREFIXES, list_objects, normalize_geometry


class UmbraCatalogTests(unittest.TestCase):
    def test_listing_decodes_paths_and_continues(self):
        session = Mock()
        responses = []
        for filename, end in [('a+b', '<NextContinuationToken>a/b+</NextContinuationToken>'),
                              ('c%2Bd', '')]:
            responses.append(('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                              f'<IsTruncated>{str(bool(end)).lower()}</IsTruncated>{end}'
                              f'<Contents><Key>sar-data/tasks/{filename}</Key><ETag>"v"</ETag>'
                              '<Size>40</Size></Contents></ListBucketResult>').encode())
        with patch('app.umbra_catalog.small_get', side_effect=responses) as get:
            objects = list(list_objects(session, 'sar-data/tasks/'))
        self.assertEqual([o.path for o in objects], ['sar-data/tasks/a b', 'sar-data/tasks/c+d'])
        self.assertEqual(get.call_args.kwargs['params']['continuation-token'], 'a/b+')

    def test_refresh_uses_public_assets_legacy_and_keeps_previous_on_failure(self):
        def obj(path, etag='"v"'):
            return Object(path, 40, etag)

        modern = PREFIXES[0] + 'task/acquisition/'
        legacy = PREFIXES[1] + 'Bingham Copper Mine/task/acquisition/'
        geometry = mapping(box(2, 3, 4, 5))
        stac = {'type': 'Feature', 'id': 'item', 'geometry': geometry, 'properties': {
            'datetime': '2026-01-13T14:11:30Z', 'platform': 'Umbra-07',
            'umbra:collect_id': 'collect', 'sar:polarizations': ['HH']}, 'assets': {
                'a': {'href': 's3://private/other.tif'}, 'b': {'href': ''}}}
        v1 = {'umbraSatelliteName': 'UMBRA_05', 'collects': [{
            'id': 'collect', 'startAtUTC': '2026-01-13T00:00:00+00:00',
            'footprintPolygonLla': geometry, 'polarizations': 'VV'}]}
        catalog_objects = {
            PREFIXES[0]: [obj(modern + name) for name in (
                'a.stac.v2.json', 'a_GEC.tif', 'a_CSI.tif', 'a_GEC_MM.tif', 'b_GEC.tif', 'a_thumbnail.png')],
            PREFIXES[1]: [obj(legacy + name) for name in ('a_METADATA.json', 'a_GEC.tif')],
            PREFIXES[2]: [obj(PREFIXES[2] + 'missing/a_GEC.tif')],
        }
        with tempfile.TemporaryDirectory() as directory:
            catalog = Catalog(Path(directory))
            with (patch('app.umbra_catalog.list_objects', side_effect=lambda _, p: catalog_objects[p]),
                  patch('app.umbra_catalog.get_sidecar', side_effect=lambda o, **_: (
                      stac if o.path.endswith('.stac.v2.json') else v1)) as get):
                catalog.refresh()
                version, scenes = catalog.search(geometry, '2026-01-13', '2026-01-14')
                self.assertEqual(len(scenes), 3)
                self.assertEqual({s['object_path'] for s in scenes}, {
                    modern + 'a_GEC.tif', modern + 'a_GEC_MM.tif', legacy + 'a_GEC.tif'})
                self.assertEqual(scenes[0]['preview_path'], modern + 'a_thumbnail.png')
                self.assertEqual(scenes[-1]['polarizations'], ['VV'])
                self.assertEqual(version['skipped'], 2)
                catalog.refresh()
                self.assertEqual(get.call_count, 2)
            with (patch('app.umbra_catalog.list_objects', side_effect=OSError('network')),
                  self.assertLogs('app.umbra_catalog', 'ERROR')):
                self.assertFalse(catalog.refresh())
            self.assertEqual(catalog.status()['state'], 'stale')
            self.assertEqual(Catalog(Path(directory)).status()['state'], 'stale')
            self.assertEqual(catalog.search(geometry, '2026-01-13', '2026-01-14')[1], scenes)

    def test_dateline_polygon_does_not_cover_greenwich(self):
        shape = normalize_geometry({'type': 'Polygon', 'coordinates': [[
            (179, 9), (-179, 9), (-179, 11), (179, 11), (179, 9)]]})
        self.assertTrue(shape.intersects(box(179.2, 9, 180, 11)))
        self.assertTrue(shape.intersects(box(-180, 9, -179.2, 11)))
        self.assertFalse(shape.intersects(box(-1, 9, 1, 11)))
