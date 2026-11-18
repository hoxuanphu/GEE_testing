import unittest


class ProfileTests(unittest.TestCase):
    def test_limits_disk_reserve_and_keys(self):
        from app.download_models import DownloadSettings
        settings=DownloadSettings()
        self.assertEqual((settings.workers,settings.backlog,settings.compression_threads),(6,12,2))
        self.assertEqual(settings.gdal_cache_bytes,256*1024*1024)
        self.assertEqual(settings.disk_reserve(100,False),300+2*1024**3)
        self.assertEqual(settings.disk_reserve(100,True),200+2*1024**3)
        for changes in ({'workers':9},{'backlog':100},{'compression_threads':0},{'gdal_cache_bytes':256}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                DownloadSettings(**changes)
