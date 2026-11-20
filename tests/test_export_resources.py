import threading
import time
import unittest


class ResourceTests(unittest.TestCase):
    def test_fifo_reentrant_and_cancelled_waiter_releases_queue(self):
        from app.export_resources import ExportResources
        resources = ExportResources()
        order = []
        errors = []
        def waiter(index, cancel=False):
            def check():
                if cancel:
                    raise InterruptedError('cancel')
            try:
                with resources.raster_admission(check):
                    order.append(index)
            except InterruptedError:
                errors.append(index)
        with resources.raster_admission():
            with resources.raster_admission():
                pass
            threads = []
            for i in range(3):
                thread = threading.Thread(target=waiter, args=(i,i==1))
                thread.start()
                threads.append(thread)
                time.sleep(.03)
            self.assertEqual(order, [])
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(order, [0,2])
        self.assertEqual(errors,[1])

    def test_existing_absolute_deadline_not_extended(self):
        from app.export_resources import ExportResources, QueueTimeout
        with self.assertRaises(QueueTimeout):
            with ExportResources().raster_admission(wait_deadline=time.monotonic()-1):
                self.fail('expired work admitted')

    def test_foreground_queue_timeout_is_an_explicit_http_error(self):
        from app import main
        from app.export_resources import RESOURCES,QueueTimeout
        from fastapi.testclient import TestClient
        from unittest import mock
        with mock.patch.object(RESOURCES,'raster_admission',side_effect=QueueTimeout('queue_timeout')), \
             TestClient(main.app,raise_server_exceptions=False) as client:
            response=client.get('/api/download/sentinel2',params=dict(lon=107,lat=10,
                start_date='2023-01-18',end_date='2023-05-17'))
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json()['code'],'queue_timeout')
