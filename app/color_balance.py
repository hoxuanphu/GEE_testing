"""Bounded overlap calibration in surface-reflectance units, not display RGB."""
from collections import Counter

import ee
import numpy as np


S2_BANDS = ['B2', 'B3', 'B4', 'B8', 'B11', 'B12', 'B1', 'B5', 'B6', 'B7', 'B8A', 'B9']
LANDSAT_BANDS = ['SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B6', 'SR_B7']
METHOD = 'clear_overlap_affine_v1'
MIN_SAMPLES = 64
MAX_SAMPLES = 4096
SAMPLE_SCALE_M = 60
MAX_SAMPLE_CALLS = 32
MAX_PASSES = 3
MIN_SPREAD = .01
MAX_RESIDUAL = .025
ALIGNED_ERROR = .002
GAIN_LIMITS = (.5, 2.)
MAX_OFFSET = .1


def fit_band(source, target) -> dict:
    """Robust affine fit; rejected fits always return identity coefficients.

    Quantiles initialize a fit; two paired-residual rejection passes remove
    sparse clouds/changed pixels before recomputing it. The residual and
    improvement checks use ALL finite pairs, not just the retained inliers.
    """
    x, y = np.asarray(source, dtype=float), np.asarray(target, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    x, y = x[valid], y[valid]
    result = dict(status='skipped', gain=1., offset=0., sample_count=int(x.size))
    if x.size < MIN_SAMPLES:
        return {**result, 'reason': 'insufficient_samples'}
    before = float(np.median(abs(y - x)))
    result['error_before'] = before
    if before <= ALIGNED_ERROR:
        return {**result, 'status': 'identity', 'error_after': before}
    keep = np.ones(x.size, dtype=bool)
    for _ in range(3):
        low_x, high_x = np.percentile(x[keep], [10, 90])
        low_y, high_y = np.percentile(y[keep], [10, 90])
        if high_x - low_x < MIN_SPREAD or high_y - low_y < MIN_SPREAD:
            return {**result, 'reason': 'flat_overlap'}
        gain = float((high_y - low_y) / (high_x - low_x))
        offset = float(np.median(y[keep] - gain * x[keep]))
        residual = y - (gain * x + offset)
        mad = float(np.median(abs(residual - np.median(residual))))
        keep = abs(residual) <= max(.01, 3 * 1.4826 * mad)
        if np.count_nonzero(keep) < MIN_SAMPLES:
            return {**result, 'reason': 'insufficient_inliers'}
    if not (GAIN_LIMITS[0] <= gain <= GAIN_LIMITS[1] and abs(offset) <= MAX_OFFSET):
        return {**result, 'reason': 'unsafe_coefficients'}
    after = float(np.median(abs(y - (gain * x + offset))))
    if after > MAX_RESIDUAL or after >= before * .9:
        return {**result, 'reason': 'poor_fit'}
    return {**result, 'status': 'applied', 'gain': gain, 'offset': offset, 'error_after': after}


def apply_coefficients(image: ee.Image, coefficients: dict) -> ee.Image:
    """Overwrite named spectral bands only; retain every original band mask."""
    bands = [band for band, fit in coefficients.items() if fit['status'] == 'applied']
    if not bands:
        return image
    adjusted = image.select(bands).multiply(ee.Image.constant(
        [coefficients[band]['gain'] for band in bands]).rename(bands)).add(ee.Image.constant(
        [coefficients[band]['offset'] for band in bands]).rename(bands)).toFloat()
    return image.addBands(adjusted, overwrite=True)


def _clear_spectra(image, dataset, bands):
    """QA affects estimation only; output is never masked by this function."""
    if dataset == 'sentinel2':
        scl = image.select('SCL')
        clear = scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(7))
    else:
        # Fill, dilated cloud, cirrus, cloud, shadow and snow.
        clear = image.select('QA_PIXEL').toInt().bitwiseAnd(63).eq(0)
    return image.select(bands).updateMask(clear)


def _sample_overlap(image, references, dataset, bands, roi):
    source = _clear_spectra(image, dataset, bands).rename(['s_' + b for b in bands])
    candidates = []
    for index, reference in reversed(list(references.items())):
        clear = _clear_spectra(reference, dataset, bands).rename(['r_' + b for b in bands])
        valid = clear.mask().reduce(ee.Reducer.min()).unmask(0, sameFootprint=False).gt(0)
        # Every paired band must come from the label's scene. Replace the
        # sample-only masks with shared binary validity: retaining fractional
        # masks here would alpha-blend spectra across reference dates.
        clear = clear.unmask(0, sameFootprint=False).updateMask(valid)
        label = ee.Image.constant(index).toInt32().rename('reference_index').updateMask(valid)
        candidates.append(clear.addBands(label))
    reference = ee.ImageCollection.fromImages(candidates).mosaic()
    # Sample-only unmask expands footprints; bound sampling with the ORIGINAL
    # reference footprints so narrow overlaps are not undersampled over a tile.
    footprints = ee.ImageCollection.fromImages(list(references.values())).geometry()
    overlap = image.geometry().intersection(footprints, ee.ErrorMargin(SAMPLE_SCALE_M))
    overlap = overlap.intersection(roi, ee.ErrorMargin(SAMPLE_SCALE_M))
    return source.addBands(reference).sample(
        region=overlap, scale=SAMPLE_SCALE_M, numPixels=MAX_SAMPLES,
        seed=42, dropNulls=True, tileScale=4, geometries=False).limit(MAX_SAMPLES)


def balance_sources(sources, dataset, roi, evaluate, scene_ids):
    """Calibrate contributors in descending priority, with a bounded retry graph.

    evaluate materializes a sample under the caller's shared aggregation lock.
    Only fully calibrated/identity scenes become references for later scenes;
    partially calibrated scenes can still be rendered with per-band status.
    """
    bands = S2_BANDS if dataset == 'sentinel2' else LANDSAT_BANDS
    images = list(sources)
    reports = [dict(scene_id=name, status='skipped', reason='insufficient_overlap') for name in scene_ids]
    report = dict(method=METHOD, spectral_values_modified=False, sample_scale_m=SAMPLE_SCALE_M,
                  max_samples=MAX_SAMPLES, min_samples=MIN_SAMPLES, scenes=reports)
    if not sources:
        return images, report
    reports[0] = dict(scene_id=scene_ids[0], status='reference')
    references = {0: sources[0]}
    pending = list(range(1, len(sources)))
    calls = 0
    for _ in range(MAX_PASSES):
        grew = False
        retry = []
        for index in pending:
            if calls >= MAX_SAMPLE_CALLS:
                reports[index]['reason'] = 'sampling_budget_exhausted'
                continue
            calls += 1
            payload = evaluate(_sample_overlap(sources[index], references, dataset, bands, roi))
            pairs = [feature['properties'] for feature in payload.get('features', [])]
            counts = Counter(row['reference_index'] for row in pairs)
            if not counts or max(counts.values()) < MIN_SAMPLES:
                retry.append(index)
                continue
            ref = max(counts, key=lambda key: (counts[key], -key))
            pairs = [row for row in pairs if row['reference_index'] == ref]
            coefficients = {band: fit_band([row['s_' + band] for row in pairs],
                                          [row['r_' + band] for row in pairs]) for band in bands}
            statuses = {fit['status'] for fit in coefficients.values()}
            status = ('partial' if 'skipped' in statuses else 'applied') if 'applied' in statuses else (
                'identity' if statuses == {'identity'} else 'skipped')
            reports[index] = dict(scene_id=scene_ids[index], reference_scene_id=scene_ids[int(ref)],
                                  status=status, bands=coefficients)
            if status == 'skipped':
                reports[index]['reason'] = 'unreliable_band_fits'
            images[index] = apply_coefficients(sources[index], coefficients)
            if status in ('applied', 'identity'):
                references[index] = images[index]
                grew = True
        if not grew or not retry:
            break
        pending = retry
    report['spectral_values_modified'] = any(row['status'] in ('applied', 'partial') for row in reports)
    report['sample_calls'] = calls
    return images, report
