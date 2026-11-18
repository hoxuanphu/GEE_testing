"""Explicit live synthetic GEE check; not part of offline discovery.

Run: .venv/Scripts/python.exe tests/live_color_balance.py
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ee

from app import imagery
from app.color_balance import S2_BANDS, LANDSAT_BANDS, _sample_overlap, balance_sources
from app.ee_session import init_ee


def validate(dataset):
    roi = ee.Geometry.Rectangle([106.44, 10.07, 106.46, 10.09], geodesic=False)
    coords = ee.Image.pixelLonLat()
    lon, lat = coords.select('longitude'), coords.select('latitude')
    gradient = lon.subtract(106.44).multiply(20).add(.05)
    bands = S2_BANDS if dataset == 'sentinel2' else LANDSAT_BANDS
    spectra = ee.Image.cat([gradient.rename(band) for band in bands]).toFloat()
    if dataset == 'sentinel2':
        quality = ee.Image.constant(4).where(lon.lt(106.444).And(lat.gt(10.08)), 9).rename('SCL')
        quality = quality.addBands(ee.Image.constant(7).rename('QA60').toUint16())
    else:
        quality = ee.Image.constant(0).where(lon.lt(106.444).And(lat.gt(10.08)), 8).rename('QA_PIXEL').toUint16()
    sources = []
    for index, name in enumerate(['fallback', 'bridge', 'primary']):
        spectral = spectra.subtract(.02).divide(1.2) if index != 2 else spectra
        image = spectral.addBands(quality).clip(roi)
        if index:
            image = image.updateMask(lon.lt(106.449 if index == 2 else 106.455))
        image = image.set({'system:index': name, 'system:time_start': 1767225600000 + index * 86400000,
                           'MGRS_TILE': name, 'WRS_PATH': index, 'WRS_ROW': 1,
                           'cloud': 10, 'hazeScore': 3 - index})
        sources.append(image)
    collection = ee.ImageCollection.fromImages(sources)
    image, components = imagery._raw_gap_fill(collection, collection, dataset, 'cloud', roi, color_balance=True)
    band = bands[0]
    evaluated = ee.Dictionary({
        'fallback': image.reduceRegion(ee.Reducer.first(), ee.Geometry.Point([106.458, 10.075]), 10),
        'primary_cloud': image.reduceRegion(ee.Reducer.first(), ee.Geometry.Point([106.442, 10.085]), 10),
        'coverage': imagery._coverage_fraction(image, dataset, roi),
        'error': image.select(band).subtract(gradient).abs().reduceRegion(
            ee.Reducer.max(), roi, 60, maxPixels=100000),
    }).getInfo()
    evaluated['components'] = components
    assert evaluated['error'][band] < .005, evaluated
    assert evaluated['coverage'] >= .99, evaluated
    quality_band = 'SCL' if dataset == 'sentinel2' else 'QA_PIXEL'
    assert evaluated['primary_cloud'][quality_band] == (9 if dataset == 'sentinel2' else 8), evaluated
    assert any(row['color_balance']['status'] == 'applied' for row in components), evaluated
    return evaluated


def validate_reference_samples():
    roi = ee.Geometry.Rectangle([106.44, 10.07, 106.46, 10.09], geodesic=False)
    def image(value):
        return ee.Image.constant([value] * 12 + [4]).rename(S2_BANDS + ['SCL']).toFloat().clip(roi)
    full = image(.4)
    missing = image(.2).addBands(ee.Image.constant(0).rename('B9').updateMask(ee.Image.constant(0)), overwrite=True)
    fractional = image(.2).updateMask(ee.Image.constant(.5))
    results = {}
    for label, primary, expected, ref in [('missing_band', missing, .4, 1), ('fractional_mask', fractional, .2, 0)]:
        sample = _sample_overlap(image(.3), {0: primary, 1: full}, 'sentinel2', S2_BANDS, roi).first().getInfo()['properties']
        assert sample['reference_index'] == ref, sample
        assert all(abs(sample['r_' + band] - expected) < 1e-6 for band in S2_BANDS), sample
        results[label] = sample
    left = image(.2).clip(ee.Geometry.Rectangle([106.44, 10.07, 106.445, 10.09]))
    right = image(.3).clip(ee.Geometry.Rectangle([106.455, 10.07, 106.46, 10.09]))
    assert _sample_overlap(right, {0: left}, 'sentinel2', S2_BANDS, roi).size().getInfo() == 0
    no_qa = image(.3).addBands(ee.Image.constant(4).rename('SCL').toFloat().updateMask(ee.Image.constant(0)), overwrite=True)
    calibrated, report = balance_sources([full, no_qa], 'sentinel2', roi, lambda sample: sample.getInfo(), ['a', 'no-qa'])
    assert calibrated[1] is no_qa, report
    assert report['scenes'][1]['reason'] == 'insufficient_overlap', report
    results['no_qa'] = report['scenes'][1]
    results['disconnected_sample_count'] = 0
    return results


if __name__ == '__main__':
    init_ee()
    results = {dataset: validate(dataset) for dataset in ('sentinel2', 'landsat')}
    references = validate_reference_samples()
    output = Path('downloads/color-balance-live-synthetic.json')
    output.write_text(json.dumps({**results, 'reference_samples': references}, indent=2), encoding='utf-8')
    print(json.dumps({key: {'coverage': value['coverage'], 'error': value['error']} for key, value in results.items()}, indent=2))
