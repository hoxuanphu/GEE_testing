"""Small explicit GEE synthetic regression, not unittest discovery."""
import json
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ee
from app import imagery
from app.ee_session import init_ee


def validate(dataset, mask):
    roi = ee.Geometry.Rectangle([106.44, 10.07, 106.46, 10.09], geodesic=False)
    lon = ee.Image.pixelLonLat().select('longitude')
    bands = imagery.S2_SPECTRAL_BANDS if dataset == 'sentinel2' else [f'SR_B{i}' for i in range(2, 8)]
    sources = {}
    for index in range(2):
        name = f'test-{index}' if dataset == 'sentinel2' else f'1_LC08_12505{index + 3}_20260101'
        spectral = ee.Image.constant([0.] + [.2] * (len(bands) - 1)).rename(bands).toFloat()
        quality = (ee.Image.constant(4).where(lon.lt(106.445), 9).rename('SCL') if dataset == 'sentinel2'
                   else ee.Image.constant(0).where(lon.lt(106.445), 8).rename('QA_PIXEL'))
        image = spectral.addBands(quality).clip(roi).updateMask(lon.lt(106.451) if index == 0 else lon.gte(106.45))
        image = image.set({'system:index': name, 'system:time_start': 1767225600000 + index * 15000,
            'SPACECRAFT_NAME': 'Sentinel-2A', 'DATATAKE_IDENTIFIER': 'one-pass', 'MGRS_TILE': name,
            'SENSING_ORBIT_NUMBER': 75, 'SPACECRAFT_ID': 'LANDSAT_8', 'WRS_PATH': 125,
            'WRS_ROW': 53 + index, 'MEAN_SOLAR_ZENITH_ANGLE': 35,
            'MEAN_SOLAR_AZIMUTH_ANGLE': 140, 'MEAN_INCIDENCE_ZENITH_ANGLE_B4': 5,
            'SUN_ELEVATION': 55, 'SUN_AZIMUTH': 140, 'ROLL_ANGLE': 0, 'cloud': 10})
        sources[name] = image
    def load(asset, _dataset):
        for name, image in sources.items():
            if asset.endswith('/' + (name if dataset == 'sentinel2' else name[2:])):
                return image
        raise AssertionError(asset)
    with mock.patch.object(imagery, 'load_timeseries_scene', side_effect=load), \
         mock.patch.object(imagery, '_load_mosaic_asset', side_effect=load):
        result = imagery._coherent_optical_scene(ee.ImageCollection.fromImages(list(sources.values())),
            dataset, 'cloud', roi, '2026-01-01', '2026-01-02', 'mosaic', 'same_pass', 3,
            mosaic_mask_clouds=mask)
    report = result.scene_grouping
    assert report['same_pass'] and report['span_days'] < .001, report
    assert report['data_coverage_pct'] >= 99, report
    assert report['output_coverage_pct'] < 99 if mask else report['output_coverage_pct'] >= 99, report
    sample = result.ee_object.reduceRegion(ee.Reducer.first(), ee.Geometry.Point([106.442, 10.08]), 10).getInfo()
    if mask:
        assert sample.get(bands[0]) is None, sample
    else:
        assert sample[bands[0]] == 0, sample
    return dict(report=report, sources=[row['scene_id'] for row in result.scenes], sample=sample)


if __name__ == '__main__':
    init_ee()
    results = {'s2_masked': validate('sentinel2', True), 's2_unmasked': validate('sentinel2', False),
               'landsat_unmasked': validate('landsat', False), 'landsat_masked': validate('landsat', True)}
    assert results['s2_masked']['sources'] == results['s2_unmasked']['sources']
    Path('downloads/coherent-mosaic-live.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps({key: value['report'] for key, value in results.items()}, indent=2))
