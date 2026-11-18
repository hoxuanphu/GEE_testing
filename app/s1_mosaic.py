"""Fill Sentinel-1 coverage gaps without mixing viewing geometries."""
import datetime as dt
from dataclasses import replace

import ee

from .imagery import S1_COLLECTION, S1_POLARIZATIONS, _OPTICAL_AGGREGATION_LOCK
from .platforms import apply_platform_filter, validate_platform

MAX_S1_FILL_SCENES = 24
COVERAGE_SCALE_M = 100


def coverage_pct(image, roi):
    valid = image.mask().reduce(ee.Reducer.min()).rename('valid')
    with _OPTICAL_AGGREGATION_LOCK:
        value = valid.unmask(0, sameFootprint=False).reduceRegion(
            reducer=ee.Reducer.mean(), geometry=roi, scale=COVERAGE_SCALE_M,
            maxPixels=100_000_000, tileScale=4,
        ).get('valid').getInfo()
    return 100 * float(value or 0)


def prepare_s1(scene, roi, start, end, polarization, merge_scenes=False, platform=None):
    """Keep the primary scene's pixels; only add candidates that increase coverage."""
    platform = validate_platform('s1gee', platform)
    primary = None
    primary_platform = scene.platform
    if platform is not None:
        primary = ee.Image(f'{S1_COLLECTION}/{scene.scene_id}')
        primary_platform = primary.toDictionary(['platform_number']).getInfo().get('platform_number')
        if primary_platform != platform:
            raise ValueError('Primary SAR platform is missing or does not match selected platform')
    bands = S1_POLARIZATIONS[polarization]
    image = scene.ee_object.select(bands)
    image = image.updateMask(image.mask().reduce(ee.Reducer.min()))
    coverage = coverage_pct(image, roi)
    used = [{'scene_id': scene.scene_id, 'date': scene.date, 'platform': primary_platform}]
    if not merge_scenes or coverage >= 99:
        return replace(scene, ee_object=image, coverage_pct=coverage,
                       scenes=tuple(used), scene_count=1)

    if primary is None:
        primary = ee.Image(f'{S1_COLLECTION}/{scene.scene_id}')
    orbit = primary.toDictionary(['orbitProperties_pass', 'relativeOrbitNumber_start',
                                  'system:time_start', 'platform_number']).getInfo()
    used[0]['platform'] = orbit.get('platform_number', primary_platform)
    if orbit.get('orbitProperties_pass') is None or orbit.get('relativeOrbitNumber_start') is None:
        raise ValueError('Cảnh SAR thiếu thông tin hướng bay/quỹ đạo; không thể ghép an toàn.')
    collection = (ee.ImageCollection(S1_COLLECTION).filterBounds(roi).filterDate(start, end)
                  .filter(ee.Filter.eq('instrumentMode', 'IW'))
                  .filter(ee.Filter.eq('orbitProperties_pass', orbit['orbitProperties_pass']))
                  .filter(ee.Filter.eq('relativeOrbitNumber_start', orbit['relativeOrbitNumber_start']))
                  .filter(ee.Filter.neq('system:index', scene.scene_id)))
    collection = apply_platform_filter(collection, 's1gee', platform)
    for band in bands:
        collection = collection.filter(ee.Filter.listContains('transmitterReceiverPolarisation', band))

    def distance(candidate):
        return candidate.set('time_distance', ee.Number(candidate.get('system:time_start'))
                             .subtract(orbit['system:time_start']).abs())

    candidates = collection.sort('system:index').map(distance).sort('time_distance').limit(MAX_S1_FILL_SCENES)
    ids = candidates.aggregate_array('system:index').getInfo()
    dates = candidates.aggregate_array('system:time_start').getInfo()
    platforms = candidates.aggregate_array('platform_number').getInfo()
    for asset_id, timestamp, candidate_platform in zip(ids, dates, platforms):
        if platform is not None and candidate_platform != platform:
            raise ValueError('Fill SAR platform is missing or does not match selected platform')
        candidate = ee.Image(f'{S1_COLLECTION}/{asset_id}').select(bands).clamp(-30, 10).float()
        candidate = candidate.updateMask(candidate.mask().reduce(ee.Reducer.min()))
        combined = candidate.blend(image)
        improved = coverage_pct(combined, roi)
        if improved <= coverage + 0.001:
            continue
        image, coverage = combined, improved
        used.append({'scene_id': asset_id, 'date': dt.datetime.fromtimestamp(
            timestamp / 1000, dt.timezone.utc).date().isoformat(),
            'orbit_pass': orbit['orbitProperties_pass'],
            'platform': candidate_platform,
            'relative_orbit': orbit['relativeOrbitNumber_start']})
        if coverage >= 99:
            break
    if len(used) == 1:
        return replace(scene, ee_object=image, coverage_pct=coverage,
                       scenes=tuple(used), scene_count=1)
    dates_used = sorted(row['date'] for row in used)
    return replace(scene, ee_object=image, coverage_pct=coverage, scenes=tuple(used),
                   scene_count=len(used), scene_id=f's1gee_mosaic_{start}_{end}',
                   date=f'{dates_used[0]} → {dates_used[-1]}')
