"""Fill Sentinel-1 coverage gaps without mixing viewing geometries."""
import datetime as dt
from dataclasses import replace

import ee

from .imagery import (S1_COLLECTION, S1_POLARIZATIONS, SceneInfo, _OPTICAL_AGGREGATION_LOCK,
                      _s1_collection)
from .platforms import apply_platform_filter, validate_platform

MAX_S1_FILL_SCENES = 24
COVERAGE_SCALE_M = 100
ORBIT_PASSES = ('ASCENDING', 'DESCENDING')
SOURCE_PROPERTIES = ['system:index', 'system:time_start', 'platform_number',
                     'orbitProperties_pass', 'relativeOrbitNumber_start']


def _valid_fraction(image, roi):
    valid = image.mask().reduce(ee.Reducer.min()).rename('valid')
    return valid.unmask(0, sameFootprint=False).gt(0).reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi, scale=COVERAGE_SCALE_M, crs='EPSG:4326',
        maxPixels=100_000_000, tileScale=4,
    ).get('valid')


def coverage_pct(image, roi):
    with _OPTICAL_AGGREGATION_LOCK:
        value = _valid_fraction(image, roi).getInfo()
    return 100 * float(value or 0)


def ranked_s1_sources(collection, roi):
    def measure(image):
        row = image.toDictionary(SOURCE_PROPERTIES).set('coverage', _valid_fraction(image, roi))
        return image.set('s1_candidate', row)

    with _OPTICAL_AGGREGATION_LOCK:
        rows = collection.map(measure).aggregate_array('s1_candidate').getInfo()
    return sorted((row for row in rows if (row.get('coverage') or 0) > 0),
                  key=lambda row: (row['coverage'], row['system:time_start'], row['system:index']),
                  reverse=True)


def _source_image(scene_id, bands):
    return ee.Image(f'{S1_COLLECTION}/{scene_id}').select(bands).clamp(-30, 10).float()


def _common_mask(image):
    return image.updateMask(image.mask().reduce(ee.Reducer.min()))


def _fill_gaps(image, candidate):
    return image.unmask(candidate, sameFootprint=False)


def search_s1_by_orbit(roi, start, end, polarization, merge_scenes, platform):
    collection = _s1_collection(start, end, polarization, platform=platform).filterBounds(roi)
    sources = ranked_s1_sources(collection, roi)
    results = {}
    for direction in ORBIT_PASSES:
        source = next((row for row in sources if row.get('orbitProperties_pass') == direction), None)
        if source is None:
            continue
        scene_id = source['system:index']
        date = dt.datetime.fromtimestamp(source['system:time_start'] / 1000, dt.timezone.utc).date().isoformat()
        primary = SceneInfo('s1gee', S1_COLLECTION, scene_id, date, -1,
                            _source_image(scene_id, S1_POLARIZATIONS[polarization]),
                            coverage_pct=100 * source['coverage'], platform=source.get('platform_number'))
        results[direction] = prepare_s1(primary, roi, start, end, polarization, merge_scenes, platform)
    return results


def restore_s1_sources(scene_ids, roi, start, end, polarization, platform, orbit_pass):
    """Validate the requested sources and recreate the selected image in display order."""
    collection = (_s1_collection(start, end, polarization, platform=platform).filterBounds(roi)
                  .filter(ee.Filter.eq('orbitProperties_pass', orbit_pass))
                  .filter(ee.Filter.inList('system:index', scene_ids)))
    rows = collection.toList(len(scene_ids)).map(
        lambda item: ee.Image(item).toDictionary(SOURCE_PROPERTIES)).getInfo()
    tracks = {row.get('relativeOrbitNumber_start') for row in rows}
    if ({row['system:index'] for row in rows} != set(scene_ids)
            or len(scene_ids) > 1 and (len(tracks) != 1 or None in tracks)):
        raise ValueError('Các cảnh đã chọn không khớp vùng, khoảng ngày, vệ tinh hoặc quỹ đạo.')
    bands = S1_POLARIZATIONS[polarization]
    image = _common_mask(_source_image(scene_ids[0], bands))
    for scene_id in scene_ids[1:]:
        image = _fill_gaps(image, _common_mask(_source_image(scene_id, bands)))
    name = f's1gee_mosaic_{start}_{end}' if len(scene_ids) > 1 else scene_ids[0]
    return SceneInfo('s1gee', S1_COLLECTION, name, start, -1, image, scene_count=len(scene_ids))


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
    image = _common_mask(scene.ee_object.select(bands))
    coverage = scene.coverage_pct if scene.coverage_pct is not None else coverage_pct(image, roi)
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
        candidate = _common_mask(_source_image(asset_id, bands))
        combined = _fill_gaps(image, candidate)
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
