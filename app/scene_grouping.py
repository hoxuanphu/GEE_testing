"""Deterministic, bounded acquisition grouping; footprint coverage is an estimate."""
import math
import json
from collections import defaultdict

from shapely.geometry import shape
from shapely.ops import unary_union

DAY_MS = 86400000
MAX_CANDIDATES = 400
MAX_GROUPS = 5
MAX_PIXEL_CHECKS = 3


def export_tags(scene):
    if not scene.get('scene_grouping'):
        return {}
    tags = dict(SCENE_GROUPING=json.dumps(scene['scene_grouping'], allow_nan=False),
                SOURCE_SCENES=json.dumps(scene.get('scenes', []), allow_nan=False))
    if scene.get('mosaic_mask_clouds') is not None:
        tags['MOSAIC_MASK_CLOUDS'] = str(scene['mosaic_mask_clouds']).lower()
        tags['COMPOSITE_METHOD'] = 'mosaic_masked' if scene['mosaic_mask_clouds'] else 'mosaic_unmasked'
    return tags


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def angle_distance(a, b):
    return abs((a - b + 180) % 360 - 180)


def acquisition_key(row, dataset):
    timestamp = row.get('system:time_start')
    if not _number(timestamp):
        return None
    if dataset == 'sentinel2':
        values = (row.get('SPACECRAFT_NAME'), row.get('DATATAKE_IDENTIFIER'))
    else:
        values = (row.get('SPACECRAFT_ID'), row.get('WRS_PATH'), int(timestamp // DAY_MS))
    return values if all(value is not None and value != '' for value in values) else None


def missing_fields(row, dataset):
    fields = (['SPACECRAFT_NAME', 'DATATAKE_IDENTIFIER', 'SENSING_ORBIT_NUMBER',
               'MEAN_SOLAR_ZENITH_ANGLE', 'MEAN_SOLAR_AZIMUTH_ANGLE', 'MEAN_INCIDENCE_ZENITH_ANGLE_B4']
              if dataset == 'sentinel2' else ['SPACECRAFT_ID', 'WRS_PATH', 'system:time_start',
                                            'SUN_ELEVATION', 'SUN_AZIMUTH', 'ROLL_ANGLE'])
    return [field for field in fields if row.get(field) is None]


def condition_distance(a, b, dataset):
    platform, orbit = ('SPACECRAFT_NAME', 'SENSING_ORBIT_NUMBER') if dataset == 'sentinel2' else ('SPACECRAFT_ID', 'WRS_PATH')
    score = sum(2 if a.get(key) is None or b.get(key) is None else int(a[key] != b[key])
                for key in (platform, orbit))
    angles = [('MEAN_SOLAR_ZENITH_ANGLE', False), ('MEAN_SOLAR_AZIMUTH_ANGLE', True),
              ('MEAN_INCIDENCE_ZENITH_ANGLE_B4', False)] if dataset == 'sentinel2' else [
                  ('SUN_ELEVATION', False), ('SUN_AZIMUTH', True), ('ROLL_ANGLE', False)]
    for key, circular in angles:
        x, y = a.get(key), b.get(key)
        score += ((angle_distance(x, y) if circular else abs(x - y)) / 10
                  if _number(x) and _number(y) else 1.)
    return score


def rank_groups(rows, dataset, mode, max_days, roi_geometry):
    if len(rows) > MAX_CANDIDATES:
        raise ValueError(f'Có quá nhiều cảnh (>{MAX_CANDIDATES}); hãy thu hẹp khoảng ngày hoặc ROI.')
    if mode not in ('near_date', 'same_pass') or max_days not in (1, 3, 5, 7):
        raise ValueError('Invalid optical scene grouping settings')
    roi = shape(roi_geometry)
    valid = sorted((i for i, row in enumerate(rows) if _number(row.get('system:time_start'))),
                   key=lambda i: (rows[i]['system:time_start'], rows[i]['system:index']))
    candidates = set()
    passes = defaultdict(list)
    for i in valid:
        key = acquisition_key(rows[i], dataset)
        if key is None:
            candidates.add((i,))
        else:
            passes[key].append(i)
    candidates.update(tuple(indices) for indices in passes.values())
    if mode == 'near_date':
        platform = 'SPACECRAFT_NAME' if dataset == 'sentinel2' else 'SPACECRAFT_ID'
        orbit = 'SENSING_ORBIT_NUMBER' if dataset == 'sentinel2' else 'WRS_PATH'
        for start in valid:
            window = [i for i in valid if 0 <= rows[i]['system:time_start'] - rows[start]['system:time_start'] <= max_days * DAY_MS]
            candidates.add(tuple(window))
            compatible = defaultdict(list)
            for i in window:
                # Unknown metadata cannot assert same platform/orbit.
                if rows[i].get(platform) is not None and rows[i].get(orbit) is not None:
                    compatible[(rows[i][platform], rows[i][orbit])].append(i)
            candidates.update(tuple(indices) for indices in compatible.values())
    footprints = {}
    for i in valid:
        geometry = rows[i].get('footprint')
        footprints[i] = shape(geometry).intersection(roi) if geometry else roi.difference(roi)
    groups = []
    for indices in candidates:
        times = [rows[i]['system:time_start'] for i in indices]
        span = (max(times) - min(times)) / DAY_MS
        # Even malformed same-pass metadata must not violate the time bound.
        if span > max_days:
            continue
        keys = [acquisition_key(rows[i], dataset) for i in indices]
        same = keys[0] is not None and all(key == keys[0] for key in keys)
        coverage = 100 * unary_union([footprints[i] for i in indices]).area / roi.area if roi.area else 0.
        conditions = sum(condition_distance(rows[indices[0]], rows[i], dataset) for i in indices) / len(indices)
        cloud = sum(rows[i].get('cloud') if _number(rows[i].get('cloud')) else 100. for i in indices) / len(indices)
        group = dict(indices=list(indices), span_days=span, same_pass=same,
                     footprint_coverage_pct=min(100., coverage), condition_distance=conditions)
        score = (coverage < 99., -round(min(coverage, 100.), 6), not same, conditions, span, cloud,
                 tuple(rows[i]['system:index'] for i in indices))
        groups.append((score, group))
    return [group for _, group in sorted(groups, key=lambda item: item[0])[:MAX_GROUPS]]
