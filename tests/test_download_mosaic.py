import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from app.imagery import merge_local_band_tiffs
from app.raster_grid import warp_to_grid


class MosaicParityTests(unittest.TestCase):
    def test_fractional_overlaps_masks_zeros_qa_and_rotated_warp_bitwise(self):
        from app.raster_mosaic import build_transport_vrt, assemble_to_grid
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            paths={}
            rng=np.random.default_rng(42)
            for band in ('B2','QA60'):
                paths[band]=[]
                for index,origin in enumerate((106.,106.+.02593)):
                    path=root/f'{band}-{index}.tif'
                    data=rng.random((517,513),dtype=np.float32)
                    if band=='QA60': data=(data*5).astype('uint8').astype('float32')
                    data[20:30,260:300]=-9999
                    data[40:50,260:300]=np.nan
                    data[0:5,260:300]=0
                    with rasterio.open(path,'w',driver='GTiff',width=513,height=517,count=1,
                        crs='EPSG:4326',transform=from_origin(origin,11,.0001,.0001),dtype='float32',nodata=-9999) as dst:
                        dst.write(data,1)
                        dst.set_band_description(1,band)
                        dst.update_tags(1,unit='source')
                        dst.scales=(.25,);dst.offsets=(.1,);dst.units=('reflectance',)
                    paths[band].append(path)
            legacy=root/'source.tif'
            merge_local_band_tiffs(paths,legacy,preserve_masks=True)
            vrt=build_transport_vrt(paths,root/'vrt')
            with rasterio.open(legacy) as expected,rasterio.open(vrt) as actual:
                self.assertEqual(actual.transform,expected.transform)
                self.assertEqual((actual.width,actual.height), (expected.width,expected.height))
                self.assertEqual(actual.tags(1),expected.tags(1))
                self.assertEqual(actual.scales,expected.scales)
                self.assertEqual(actual.offsets,expected.offsets)
                self.assertEqual(actual.units,expected.units)
                for band in range(1,3):
                    a,b=actual.read(band,masked=True),expected.read(band,masked=True)
                    np.testing.assert_array_equal(np.ma.getmaskarray(a),np.ma.getmaskarray(b))
                    valid=~np.ma.getmaskarray(b)
                    np.testing.assert_array_equal(a.data[valid].view('uint32'),b.data[valid].view('uint32'))
            grid=dict(crs='EPSG:4326',transform=[.0001,.00001,106.002,.00001,-.0001,10.998],width=700,height=450)
            expected,actual=root/'expected.tif',root/'actual.tif'
            warp_to_grid(legacy,expected,grid)
            from unittest import mock
            from app import raster_grid
            warp=raster_grid.WarpedVRT
            counts=[]
            def observe(source,**kwargs):
                counts.append(source.count)
                return warp(source,**kwargs)
            with mock.patch.object(raster_grid,'WarpedVRT',side_effect=observe):
                assemble_to_grid(paths,actual,grid)
            self.assertEqual(counts,[2,2],'fast assembly must reuse one full-band value/validity warp pair')
            with rasterio.open(expected) as a,rasterio.open(actual) as b:
                np.testing.assert_array_equal(a.read_masks(),b.read_masks())
                valid=a.read_masks()>0
                np.testing.assert_array_equal(a.read()[valid].view('uint32'),b.read()[valid].view('uint32'))
            self.assertFalse(list(root.glob('actual.source.tif')))

    def test_cross_crs_rotated_26_bands_with_missing_qa_matches_legacy(self):
        from app.raster_mosaic import assemble_to_grid
        from app.raster_grid import build_rotated_grid
        from app.imagery import s2_export_bands,S2_CATEGORICAL_BANDS
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);paths={};rng=np.random.default_rng(101)
            for name in s2_export_bands(None):
                path=root/(name+'.tif');paths[name]=[path]
                values=rng.random((258,262),dtype=np.float32)
                if name in S2_CATEGORICAL_BANDS:values=(values*11).astype('uint8').astype('float32')
                if name.startswith('QA'):values[:]=-9999
                with rasterio.open(path,'w',driver='GTiff',width=262,height=258,count=1,dtype='float32',
                    crs='EPSG:4326',transform=from_origin(106.43203553636725,10.087990809133814,
                        10/111319.49079327358,10/111319.49079327358),nodata=-9999) as dst:
                    dst.write(values,1)
            grid=build_rotated_grid(106.4437867045,10.076415139,1000,15,10)
            source,expected,actual=root/'source.tif',root/'expected.tif',root/'actual.tif'
            merge_local_band_tiffs(paths,source,preserve_masks=True);warp_to_grid(source,expected,grid)
            assemble_to_grid(paths,actual,grid)
            with rasterio.open(expected) as a,rasterio.open(actual) as b:
                np.testing.assert_array_equal(a.read_masks(),b.read_masks())
                valid=a.read_masks()>0
                np.testing.assert_array_equal(a.read()[valid].view('uint32'),b.read()[valid].view('uint32'))
