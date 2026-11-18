"""Explicit live GEE regression: python tests/live_raw_seam.py (not offline discovery)."""
import json
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ee
from app import imagery
from app.ee_session import init_ee


def run_validation():
    init_ee()
    roi = ee.Geometry.Rectangle([106.44, 10.07, 106.45, 10.08], geodesic=False)
    lon = ee.Image.pixelLonLat().select('longitude')
    bands = ['B2', 'B3', 'B4', 'B8', 'B11', 'B12']
    sources = []
    for index in range(4):
        values = [.8, .8, .8, 0, .8, .8] if index == 3 else [.2] * 6
        image = ee.Image.constant(values).rename(bands).toFloat().clip(roi)
        if index == 3:
            image = image.updateMask(lon.lt(106.445).multiply(136 / 255))
        qa = ee.Image.constant(7).toUint16().rename('QA60')
        if index == 3:
            qa = qa.toFloat().updateMask(ee.Image.constant(0))
        sources.append(image.addBands(qa).set({
            'system:index': f'source-{index}', 'system:time_start': 1674000000000 + index * 86400000,
            'MGRS_TILE': 'fallback-tile' if index == 0 else 'preferred-tile',
            'cloud': 10, 'hazeScore': 4 - index,
        }))
    collection = ee.ImageCollection.fromImages(sources)
    with mock.patch.object(imagery, '_haze_rank', return_value=collection):
        image, components = imagery._least_cloudy_per_tile(
            collection, 'sentinel2', 'cloud', roi, keep_clouds=True)
    def sample(lon_value):
        return image.reduceRegion(ee.Reducer.first(), ee.Geometry.Point([lon_value, 10.075]), 10)
    result = ee.Dictionary({
        'preferred': sample(106.442), 'fallback': sample(106.448),
        'coverage': imagery._coverage_fraction(image, 'sentinel2', roi),
    }).getInfo()
    result['components'] = components
    print(json.dumps(result, indent=2), flush=True)
    assert result['preferred'].get('B2') is not None, 'Fractional source mask created an artificial data gap'
    assert abs(result['preferred']['B2'] - .8) < 1e-6, 'Cloudy preferred pixel must stay unchanged'
    assert result['preferred']['B8'] == 0, 'Valid spectral zero must remain valid'
    assert result['preferred'].get('QA60') is None, 'Missing preferred QA cannot be filled from another source'
    assert abs(result['fallback']['B2'] - .2) < 1e-6
    assert result['fallback']['QA60'] == 7
    assert result['coverage'] >= .99
    assert {c['scene_id'] for c in components} == {'source-0', 'source-3'}
    Path('downloads/raw-seam-fix-live-synthetic.json').write_text(json.dumps(result, indent=2), encoding='utf-8')


if __name__ == '__main__':
    run_validation()
