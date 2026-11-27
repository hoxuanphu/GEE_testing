"""Fill Sentinel-1 coverage gaps without mixing viewing geometries."""
import datetime as dt
from dataclasses import replace
import json

import ee
from rasterio.warp import transform_geom
from shapely.geometry import mapping, shape
from shapely.prepared import prep

from .imagery import (S1_COLLECTION, S1_POLARIZATIONS, SceneInfo, _OPTICAL_AGGREGATION_LOCK,
                      _s1_collection)
from .platforms import apply_platform_filter, validate_platform

MAX_S1_FILL_SCENES = 24
COVERAGE_SCALE_M = 100
ORBIT_PASSES = ('ASCENDING', 'DESCENDING')
SOURCE_PROPERTIES = ['system:index', 'system:time_start', 'platform_number',
                     'orbitProperties_pass', 'relativeOrbitNumber_start',
                     'transmitterReceiverPolarisation']


def province_search_roi(province):
    """Reduce the inline GEE graph before measuring SAR coverage at 100 m."""
    west, east = province.bbox.west, province.bbox.east
    zone = int(((west + east) / 2 + 180) // 6) + 1
    local_crs = f'EPSG:{32600 + zone}'
    projected = shape(transform_geom('EPSG:4326', local_crs, province.geometry))
    compact = projected.simplify(COVERAGE_SCALE_M / 4, preserve_topology=True)
    geometry = transform_geom(local_crs, 'EPSG:4326', mapping(compact))
    return ee.Geometry(geometry, proj='EPSG:4326', geodesic=False)


def _source_tags(sources):
    return {
        'SAR_SOURCE_COUNT': str(len(sources)),
        'SAR_SOURCES': json.dumps([{key: value for key, value in source.items() if key != 'footprint'}
                                   for source in sources], separators=(',', ':')),
    }


def mosaic_tags(sources, *, provider, policy):
    return {'SAR_MOSAIC_PROVIDER': provider, 'SAR_MOSAIC_POLICY': policy, **_source_tags(sources)}


def s1_export_tags(scene, polarization, *, extent, selection_mode=None):
    """Metadata for the source scenes, separate from the output TIFF grid."""
    sources = getattr(scene, 'scenes', ())
    if not sources:
        source = {'scene_id': scene.scene_id, 'asset_id': f'{S1_COLLECTION}/{scene.scene_id}',
                  'date': scene.date}
        if getattr(scene, 'platform', None):
            source['platform'] = scene.platform
        sources = (source,)
    tags = {'SOURCE_COLLECTION': S1_COLLECTION, 'EXPORT_EXTENT': extent, 'VALUE_UNITS': 'dB',
            'SAR_POLARIZATION': polarization, 'SAR_INSTRUMENT_MODE': 'IW'}
    if selection_mode is not None:
        tags['SAR_SELECTION_MODE'] = selection_mode
    if len(sources) > 1:
        tags.update(mosaic_tags(sources, provider='GEE', policy='primary_first_fill_nodata'))
    else:
        source = sources[0]
        tags['SOURCE_ASSET'] = source.get('asset_id') or f'{S1_COLLECTION}/{source["scene_id"]}'
        tags.update(_source_tags(sources))
    footprints = {source['scene_id']: source['footprint'] for source in sources if source.get('footprint')}
    if footprints:
        tags['SAR_SOURCE_FOOTPRINTS'] = json.dumps(footprints, separators=(',', ':'))
    return tags


def write_s1_export_tags(path, scene, polarization, *, extent, selection_mode=None):
    from rasterio import open as open_raster

    with open_raster(path, 'r+') as raster:
        raster.update_tags(**s1_export_tags(scene, polarization, extent=extent, selection_mode=selection_mode))


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


def _scene_geometry(image):
    return image.geometry(ee.ErrorMargin(30), ee.Projection('EPSG:4326'), False)


def ranked_s1_sources(collection, roi, *, include_footprint=False):
    def measure(image):
        row = image.toDictionary(SOURCE_PROPERTIES).set('coverage', _valid_fraction(image, roi))
        if include_footprint:
            row = row.set('footprint', _scene_geometry(image))
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
        metadata = _source_metadata(source)
        scene_id = metadata['scene_id']
        primary = SceneInfo('s1gee', S1_COLLECTION, scene_id, metadata['date'], -1,
                            _source_image(scene_id, S1_POLARIZATIONS[polarization]), scenes=(metadata,),
                            coverage_pct=100 * source['coverage'], platform=metadata['platform'])
        results[direction] = prepare_s1(primary, roi, start, end, polarization, merge_scenes, platform)
    return results


def _source_metadata(row):
    timestamp = dt.datetime.fromtimestamp(row['system:time_start'] / 1000, dt.timezone.utc)
    scene_id = row['system:index']
    source = {
        'scene_id': scene_id, 'asset_id': f'{S1_COLLECTION}/{scene_id}',
        'date': timestamp.date().isoformat(), 'timestamp': timestamp.isoformat(),
        'platform': row.get('platform_number'), 'orbit_pass': row.get('orbitProperties_pass'),
        'relative_orbit': row.get('relativeOrbitNumber_start'),
        'footprint': row.get('footprint'),
    }
    if row.get('transmitterReceiverPolarisation'):
        source['source_polarizations'] = row['transmitterReceiverPolarisation']
    return source


def list_s1_results(roi, start, end, polarization, merge_scenes, platform):
    collection = _s1_collection(start, end, polarization, platform=platform).filterBounds(roi)
    with _OPTICAL_AGGREGATION_LOCK:
        count = collection.limit(501).size().getInfo()
    if count > 500:
        raise ValueError('Có hơn 500 cảnh SAR. Hãy thu hẹp khoảng ngày hoặc vùng tìm.')
    rows = ranked_s1_sources(collection, roi, include_footprint=True)
    metadata = {row['system:index']: _source_metadata(row) for row in rows}
    results = []
    for direction in ORBIT_PASSES:
        choices = []
        for row in rows:
            if row.get('orbitProperties_pass') != direction:
                continue
            source = metadata[row['system:index']]
            scene = SceneInfo('s1gee', S1_COLLECTION, source['scene_id'], source['date'], -1,
                              _source_image(source['scene_id'], S1_POLARIZATIONS[polarization]),
                              scenes=(source,), scene_count=1, coverage_pct=100 * row['coverage'],
                              platform=source['platform'])
            choices.append(scene)
        if merge_scenes and choices:
            mosaic = prepare_s1(choices[0], roi, start, end, polarization, True, platform)
            if mosaic.scene_count > 1:
                sources = tuple({**item, **metadata.get(item['scene_id'], {})} for item in mosaic.scenes)
                results.append((direction, replace(mosaic, scenes=sources)))
        results.extend((direction, scene) for scene in choices)
    return results


def restore_s1_sources(scene_ids, roi, start, end, polarization, platform, orbit_pass, *,
                       province_geometry=None):
    """Validate the requested sources and recreate the selected image in display order."""
    collection = _s1_collection(start, end, polarization, platform=platform)
    if roi is not None:
        collection = collection.filterBounds(roi)
    if orbit_pass is not None:
        collection = collection.filter(ee.Filter.eq('orbitProperties_pass', orbit_pass))
    collection = collection.filter(ee.Filter.inList('system:index', scene_ids))
    def source_row(item):
        image = ee.Image(item)
        metadata = image.toDictionary(SOURCE_PROPERTIES)
        if roi is None or province_geometry is not None:
            metadata = metadata.set('footprint', _scene_geometry(image))
        return metadata

    rows = collection.toList(len(scene_ids)).map(source_row).getInfo()
    tracks = {row.get('relativeOrbitNumber_start') for row in rows}
    if ({row['system:index'] for row in rows} != set(scene_ids)
            or len(scene_ids) > 1 and (len(tracks) != 1 or None in tracks)):
        raise ValueError('Các cảnh đã chọn không khớp vùng, khoảng ngày, vệ tinh hoặc quỹ đạo.')
    if province_geometry is not None:
        province = prep(shape(province_geometry))
        for row in rows:
            footprint = row.get('footprint')
            if footprint is None or not province.intersects(shape(footprint)):
                raise ValueError('Một cảnh SAR đã chọn không giao với ranh giới tỉnh.')
    bands = S1_POLARIZATIONS[polarization]
    image = _common_mask(_source_image(scene_ids[0], bands))
    for scene_id in scene_ids[1:]:
        image = _fill_gaps(image, _common_mask(_source_image(scene_id, bands)))
    name = f's1gee_mosaic_{start}_{end}' if len(scene_ids) > 1 else scene_ids[0]
    by_id = {row['system:index']: row for row in rows}
    sources = tuple(_source_metadata(by_id[scene_id]) for scene_id in scene_ids)
    return SceneInfo('s1gee', S1_COLLECTION, name, sources[0]['date'], -1, image,
                     scene_count=len(scene_ids), scenes=sources)


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
    primary_source = dict(scene.scenes[0]) if scene.scenes else {}
    primary_source.update(scene_id=scene.scene_id, date=scene.date, platform=primary_platform)
    used = [primary_source]
    if not merge_scenes or coverage >= 99:
        return replace(scene, ee_object=image, coverage_pct=coverage,
                       scenes=tuple(used), scene_count=1)

    if primary is None:
        primary = ee.Image(f'{S1_COLLECTION}/{scene.scene_id}')
    orbit = primary.toDictionary(['orbitProperties_pass', 'relativeOrbitNumber_start',
                                  'system:time_start', 'platform_number']).getInfo()
    if orbit.get('orbitProperties_pass') is None or orbit.get('relativeOrbitNumber_start') is None:
        raise ValueError('Cảnh SAR thiếu thông tin hướng bay/quỹ đạo; không thể ghép an toàn.')
    used[0] = _source_metadata({'platform_number': primary_platform, **orbit, 'system:index': scene.scene_id})
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
        used.append(_source_metadata({
            **orbit, 'system:index': asset_id, 'system:time_start': timestamp,
            'platform_number': candidate_platform,
        }))
        if coverage >= 99:
            break
    if len(used) == 1:
        return replace(scene, ee_object=image, coverage_pct=coverage,
                       scenes=tuple(used), scene_count=1)
    dates_used = sorted(row['date'] for row in used)
    return replace(scene, ee_object=image, coverage_pct=coverage, scenes=tuple(used),
                   scene_count=len(used), scene_id=f's1gee_mosaic_{start}_{end}',
                   date=f'{dates_used[0]} → {dates_used[-1]}')
